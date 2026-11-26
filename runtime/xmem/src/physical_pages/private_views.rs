//! Private anonymous placeholder mappings and lazy section promotion for
//! XMem physical pages with a single CPU view.
use super::{PAGE, Pool, shared_pages};
use super::backing::{DeferredKind, PageBacking};
use core::ptr;
use std::collections::HashSet;
use windows_sys::Win32::Foundation::{
    ERROR_BUSY, ERROR_INVALID_ADDRESS, ERROR_NOT_ENOUGH_MEMORY,
};
use windows_sys::Win32::System::Memory::{MEM_RESERVE, PAGE_NOACCESS};

impl Pool {
    pub(super) fn populate_page_bytes(
        &self,
        token: u64,
        dst: *mut u8,
        zero_if_empty: bool,
    ) -> Result<(), u32> {
        let page = &self.pages[&token];
        match &page.backing {
            PageBacking::PrivateView(src) => {
                // SAFETY: `src` is a live `PAGE_READWRITE` 64 KiB page and `dst` holds `PAGE` bytes.
                unsafe { ptr::copy_nonoverlapping(*src as *const u8, dst, PAGE) };
                Ok(())
            }
            PageBacking::PrivateArena { arena, offset } => {
                unsafe { ptr::copy_nonoverlapping((arena.base() + *offset) as *const u8, dst, PAGE) };
                Ok(())
            }
            PageBacking::SharedSection => page.section.read_into(page.offset, dst),
            PageBacking::Unbacked => {
                if !page.saved.is_empty() {
                    unsafe { ptr::copy_nonoverlapping(page.saved.as_ptr(), dst, PAGE) };
                    Ok(())
                } else if page.has_section_data {
                    page.section.read_into(page.offset, dst)
                } else {
                    if zero_if_empty {
                        unsafe { ptr::write_bytes(dst, 0, PAGE) };
                    }
                    Ok(())
                }
            }
        }
    }

    pub(super) fn clear_page_mapping(&mut self, at: usize, save_bytes: bool) {
        let Some(token) = self.mappings.remove(&at) else {
            return;
        };
        let was_gpu = self.gpu_aliases.remove(&at);
        let page = self.pages.get_mut(&token).expect("mapping owns a live page");
        if matches!(page.backing, PageBacking::PrivateView(view_va) if view_va == at) {
            if save_bytes {
                let mut saved = Vec::new();
                if saved.try_reserve_exact(PAGE).is_ok() {
                    saved.resize(PAGE, 0);
                    unsafe { ptr::copy_nonoverlapping(at as *const u8, saved.as_mut_ptr(), PAGE) };
                    page.saved = saved;
                }
            }
            page.backing = PageBacking::Unbacked;
        }
        page.aliases -= 1;
        if !was_gpu {
            page.cpu_views -= 1;
        }
    }

    fn detach_private_slot(&mut self, at: usize) -> Result<(), u32> {
        let Some((&lo, &hi)) = self.private_views.range(..=at).next_back() else {
            return Err(ERROR_INVALID_ADDRESS);
        };
        if at >= hi
            || self.has_hold(lo, hi)
            || (lo..hi).step_by(PAGE).any(|va| self.deferred_unmaps.contains_key(&va))
        {
            return Err(ERROR_BUSY);
        }
        let mut left_buf = Vec::new();
        let mut right_buf = Vec::new();
        let left_len = at - lo;
        let right_start = at + PAGE;
        let right_len = hi - right_start;
        if left_len > 0 {
            left_buf
                .try_reserve_exact(left_len)
                .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
            left_buf.resize(left_len, 0);
            unsafe { ptr::copy_nonoverlapping(lo as *const u8, left_buf.as_mut_ptr(), left_len) };
        }
        if right_len > 0 {
            right_buf
                .try_reserve_exact(right_len)
                .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
            right_buf.resize(right_len, 0);
            unsafe {
                ptr::copy_nonoverlapping(right_start as *const u8, right_buf.as_mut_ptr(), right_len)
            };
        }
        shared_pages::unmap_private_range(lo, hi - lo)?;
        self.private_views.remove(&lo);
        for &(sub_lo, sub_len, ref buf) in &[(lo, left_len, left_buf), (right_start, right_len, right_buf)] {
            if sub_len == 0 {
                continue;
            }
            shared_pages::isolate_range(sub_lo, sub_len, &shared_pages::region(sub_lo)?)?;
            shared_pages::map_private_range(sub_lo, sub_len)?;
            unsafe { ptr::copy_nonoverlapping(buf.as_ptr(), sub_lo as *mut u8, sub_len) };
            self.private_views.insert(sub_lo, sub_lo + sub_len);
        }
        Ok(())
    }

    pub(super) fn promote_to_section(&mut self, token: u64) -> Result<(), u32> {
        let existing_at = match self.pages[&token].backing {
            PageBacking::PrivateView(at) => at,
            PageBacking::SharedSection => return Ok(()),
            _ => return Err(ERROR_INVALID_ADDRESS),
        };
        let mut saved = vec![0u8; PAGE];
        // SAFETY: `existing_at` is a committed 64 KiB `PAGE_READWRITE` CPU view.
        unsafe { ptr::copy_nonoverlapping(existing_at as *const u8, saved.as_mut_ptr(), PAGE) };
        {
            let page = &self.pages[&token];
            page.section.write_at(page.offset, saved.as_ptr(), PAGE)?;
        }
        self.detach_private_slot(existing_at)?;
        let info = shared_pages::region(existing_at)?;
        shared_pages::isolate_page(existing_at, &info)?;
        let page = self.pages.get_mut(&token).expect("token validated");
        page.section.map(page.offset, existing_at)?;
        page.has_section_data = true;
        page.saved = Vec::new();
        page.backing = PageBacking::SharedSection;
        Ok(())
    }

    pub(super) fn map_private_page(
        &mut self,
        at: usize,
        token: u64,
        gpu_alias: bool,
    ) -> Result<bool, u32> {
        if matches!(self.deferred_unmaps.get(&at), Some(DeferredKind::PrivatePlaceholder)) {
            self.populate_page_bytes(token, at as *mut u8, true)?;
            self.deferred_unmaps.remove(&at);
            self.mappings.insert(at, token);
            if gpu_alias {
                self.gpu_aliases.insert(at);
            }
            let page = self.pages.get_mut(&token).expect("validated token");
            page.aliases += 1;
            if !gpu_alias {
                page.cpu_views += 1;
                page.backing = PageBacking::PrivateView(at);
                page.saved = Vec::new();
            }
            return Ok(true);
        }
        let existing = self.aperture(at);
        let info = shared_pages::region(at)?;
        if info.State != MEM_RESERVE || info.AllocationProtect != PAGE_NOACCESS {
            return if existing.is_some() {
                Err(ERROR_INVALID_ADDRESS)
            } else {
                Ok(false)
            };
        }
        let (base, extent) = existing.unwrap_or((
            info.AllocationBase.addr(),
            info.BaseAddress.addr() + info.RegionSize - info.AllocationBase.addr(),
        ));
        if let Err(e) = shared_pages::isolate_page(at, &info) {
            return if existing.is_some() { Err(e) } else { Ok(false) };
        }
        if let Err(e) = shared_pages::map_private_range(at, PAGE) {
            if existing.is_some() {
                return Err(e);
            }
            if extent == PAGE {
                return Ok(false);
            }
            shared_pages::coalesce(base, extent)?;
            return Err(e);
        }
        if let Err(e) = self.populate_page_bytes(token, at as *mut u8, false) {
            let _ = shared_pages::unmap_private_range(at, PAGE);
            if existing.is_none() {
                let _ = shared_pages::coalesce(base, extent);
            }
            return Err(e);
        }
        self.private_views.insert(at, at + PAGE);
        self.mappings.insert(at, token);
        if gpu_alias {
            self.gpu_aliases.insert(at);
        }
        self.apertures.entry(base).or_insert((extent, 0)).1 += 1;
        let page = self.pages.get_mut(&token).expect("validated token");
        page.aliases += 1;
        if !gpu_alias {
            page.cpu_views += 1;
            page.backing = PageBacking::PrivateView(at);
            page.saved = Vec::new();
        }
        Ok(true)
    }

    pub(super) fn map_private_contiguous(
        &mut self,
        start: usize,
        tokens: &[u64],
        gpu_alias: bool,
    ) -> Result<bool, u32> {
        if tokens.len() < 2 {
            return Ok(false);
        }
        let bytes = tokens.len() * PAGE;
        if (start..start + bytes)
            .step_by(PAGE)
            .any(|va| self.deferred_unmaps.contains_key(&va))
        {
            return Ok(false);
        }
        let mut seen_cpu = HashSet::new();
        for &token in tokens {
            let p = &self.pages[&token];
            if p.mapped
                || matches!(p.backing, PageBacking::SharedSection)
                || (!gpu_alias && (p.cpu_views != 0 || !seen_cpu.insert(token)))
            {
                return Ok(false);
            }
        }
        let info = shared_pages::region(start)?;
        if info.State != MEM_RESERVE
            || info.AllocationProtect != PAGE_NOACCESS
            || start + bytes > info.BaseAddress.addr() + info.RegionSize
        {
            return Ok(false);
        }
        let raw_base = info.AllocationBase.addr();
        let (base, extent) = self
            .aperture(start)
            .unwrap_or((raw_base, info.BaseAddress.addr() + info.RegionSize - raw_base));
        if shared_pages::isolate_range(start, bytes, &info).is_err() {
            return Ok(false);
        }
        if let Err(e) = shared_pages::map_private_range(start, bytes) {
            if self.aperture(start).is_none() && start == base && bytes == extent {
                return Ok(false);
            }
            if self.aperture(start).is_none() {
                shared_pages::coalesce(base, extent)?;
            }
            return Err(e);
        }
        for (i, &token) in tokens.iter().enumerate() {
            if let Err(e) = self.populate_page_bytes(token, (start + i * PAGE) as *mut u8, false) {
                let _ = shared_pages::unmap_private_range(start, bytes);
                if self.aperture(start).is_none() {
                    let _ = shared_pages::coalesce(base, extent);
                }
                return Err(e);
            }
        }
        self.private_views.insert(start, start + bytes);
        self.apertures.entry(base).or_insert((extent, 0)).1 += tokens.len();
        for (i, &token) in tokens.iter().enumerate() {
            let at = start + i * PAGE;
            self.mappings.insert(at, token);
            if gpu_alias {
                self.gpu_aliases.insert(at);
            }
            let p = self.pages.get_mut(&token).unwrap();
            p.aliases += 1;
            if !gpu_alias {
                p.cpu_views += 1;
                p.backing = PageBacking::PrivateView(at);
                p.saved = Vec::new();
            }
        }
        Ok(true)
    }

    pub(super) fn unmap_private_view_range(
        &mut self,
        at: usize,
        bytes: usize,
        save_bytes: bool,
    ) -> Result<bool, u32> {
        let Some((&lo, &hi)) = self.private_views.range(..=at).next_back() else {
            return Ok(false);
        };
        let end = at + bytes;
        if at >= hi || end > hi {
            return Ok(false);
        }
        let (base, extent) = self.aperture(at).ok_or(ERROR_INVALID_ADDRESS)?;
        if (at..end).step_by(PAGE).any(|va| !self.mappings.contains_key(&va)) {
            return Err(ERROR_INVALID_ADDRESS);
        }
        if self.has_hold(lo, hi) {
            for va in (at..end).step_by(PAGE) {
                self.clear_page_mapping(va, save_bytes);
                self.deferred_unmaps.insert(va, DeferredKind::PrivatePlaceholder);
            }
            return Ok(true);
        }
        let mut restored: Vec<(usize, usize, Vec<u8>)> = Vec::new();
        for &(sub_lo, sub_hi) in &[(lo, at), (end, hi)] {
            if sub_lo < sub_hi {
                let len = sub_hi - sub_lo;
                let mut buf = Vec::new();
                buf.try_reserve_exact(len).map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
                buf.resize(len, 0);
                unsafe { ptr::copy_nonoverlapping(sub_lo as *const u8, buf.as_mut_ptr(), len) };
                restored.push((sub_lo, sub_hi, buf));
            }
        }
        for va in (at..end).step_by(PAGE) {
            self.clear_page_mapping(va, save_bytes);
        }
        shared_pages::unmap_private_range(lo, hi - lo)?;
        self.private_views.remove(&lo);
        for (sub_lo, sub_hi, buf) in restored {
            let len = sub_hi - sub_lo;
            shared_pages::isolate_range(sub_lo, len, &shared_pages::region(sub_lo)?)?;
            shared_pages::map_private_range(sub_lo, len)?;
            unsafe { ptr::copy_nonoverlapping(buf.as_ptr(), sub_lo as *mut u8, len) };
            self.private_views.insert(sub_lo, sub_hi);
        }
        self.release_aperture_pages(base, extent, bytes / PAGE);
        Ok(true)
    }
}
