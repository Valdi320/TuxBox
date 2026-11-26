//! Private anonymous placeholder mappings and VEH-triggered lazy section
//! promotion for XMem physical pages with one canonical `MEM_PRIVATE` backing.
use super::{PAGE, POOL, Pool, PrivateSpan, shared_pages};
use super::backing::{DeferredKind, PageBacking};
use core::ptr;
use std::collections::HashSet;
use std::sync::Once;
use windows_sys::Win32::Foundation::{
    ERROR_BUSY, ERROR_INVALID_ADDRESS, ERROR_INVALID_PARAMETER, ERROR_NOT_ENOUGH_MEMORY,
    EXCEPTION_ACCESS_VIOLATION,
};
use windows_sys::Win32::System::Diagnostics::Debug::{
    AddVectoredExceptionHandler, EXCEPTION_POINTERS,
};
use windows_sys::Win32::System::Memory::{MEM_RESERVE, PAGE_NOACCESS, PAGE_READWRITE};

#[derive(Clone, Copy)]
enum SlotRole {
    GpuAlias,
    Canonical,
    Upgrade(usize),
    SecondaryAlias,
}

static VEH_INIT: Once = Once::new();

fn ensure_secondary_alias_veh() {
    VEH_INIT.call_once(|| {
        unsafe { AddVectoredExceptionHandler(1, Some(secondary_alias_veh)) };
    });
}

unsafe extern "system" fn secondary_alias_veh(info: *mut EXCEPTION_POINTERS) -> i32 {
    if info.is_null() {
        return 0;
    }
    let record = unsafe { (*info).ExceptionRecord };
    if record.is_null() {
        return 0;
    }
    let rec = unsafe { &*record };
    if rec.ExceptionCode != EXCEPTION_ACCESS_VIOLATION || rec.NumberParameters < 2 {
        return 0;
    }
    let fault_page = rec.ExceptionInformation[1] & !(PAGE - 1);
    let mut pool = POOL.lock().unwrap_or_else(|e| e.into_inner());
    if !pool.secondary_aliases.contains(&fault_page) {
        return 0;
    }
    if pool.promote_faulting_alias(fault_page).is_ok() {
        -1
    } else {
        0
    }
}

fn protect_runs(pages: &mut [usize], protect: u32) -> Result<(), u32> {
    if pages.is_empty() {
        return Ok(());
    }
    pages.sort_unstable();
    let mut run_lo = pages[0];
    let mut run_len = PAGE;
    for &va in &pages[1..] {
        if va == run_lo + run_len {
            run_len += PAGE;
        } else {
            shared_pages::protect_private_range(run_lo, run_len, protect)?;
            run_lo = va;
            run_len = PAGE;
        }
    }
    shared_pages::protect_private_range(run_lo, run_len, protect)
}

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
                // SAFETY: `src` is the canonical `PAGE_READWRITE` 64 KiB page.
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

    fn classify_private_slot(&self, at: usize, token: u64, gpu_alias: bool) -> SlotRole {
        if gpu_alias {
            return SlotRole::GpuAlias;
        }
        let is_cpu = self.is_cpu_aperture(at);
        let page = &self.pages[&token];
        match page.backing {
            PageBacking::Unbacked => SlotRole::Canonical,
            PageBacking::PrivateView(old_va) if is_cpu && !page.backing_is_cpu => {
                SlotRole::Upgrade(old_va)
            }
            PageBacking::PrivateView(_) | PageBacking::PrivateArena { .. } => {
                SlotRole::SecondaryAlias
            }
            PageBacking::SharedSection => unreachable!("shared section handled separately"),
        }
    }

    pub(super) fn clear_page_mapping(&mut self, at: usize, save_bytes: bool) {
        self.secondary_aliases.remove(&at);
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
            page.backing_is_cpu = false;
        }
        page.aliases -= 1;
        if !was_gpu {
            page.cpu_views -= 1;
        }
    }

    pub(super) fn detach_private_slot(
        &mut self,
        at: usize,
        release_slot_from_aperture: bool,
    ) -> Result<(), u32> {
        let Some((lo, span)) = self.private_span(at) else {
            return Err(ERROR_INVALID_ADDRESS);
        };
        if self.has_hold(lo, span.end)
            || (lo..span.end).step_by(PAGE).any(|va| self.deferred_unmaps.contains_key(&va))
        {
            return Err(ERROR_BUSY);
        }
        let base_and_extent = self.aperture(lo);
        let mut live_runs = self.collect_live_runs(lo, at);
        live_runs.extend(self.collect_live_runs(at + PAGE, span.end));
        let mut snapshots = Vec::new();
        for &(r_lo, r_hi) in &live_runs {
            let len = r_hi - r_lo;
            let mut buf = Vec::new();
            buf.try_reserve_exact(len)
                .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
            buf.resize(len, 0);
            let mut masked = Vec::new();
            for (idx, va) in (r_lo..r_hi).step_by(PAGE).enumerate() {
                if self.secondary_aliases.contains(&va) {
                    masked.push(va);
                } else {
                    unsafe {
                        ptr::copy_nonoverlapping(
                            va as *const u8,
                            buf.as_mut_ptr().add(idx * PAGE),
                            PAGE,
                        )
                    };
                }
            }
            snapshots.push((r_lo, r_hi, buf, masked));
        }
        shared_pages::unmap_private_range(lo, span.end - lo)?;
        self.private_views.remove(&lo);
        let mut restored_pages = 0usize;
        for (sub_lo, sub_hi, buf, mut masked) in snapshots {
            let sub_len = sub_hi - sub_lo;
            shared_pages::isolate_range(sub_lo, sub_len, &shared_pages::region(sub_lo)?)?;
            shared_pages::map_private_range(sub_lo, sub_len)?;
            unsafe { ptr::copy_nonoverlapping(buf.as_ptr(), sub_lo as *mut u8, sub_len) };
            protect_runs(&mut masked, PAGE_NOACCESS)?;
            let count = sub_len / PAGE;
            self.private_views.insert(
                sub_lo,
                PrivateSpan {
                    end: sub_hi,
                    mapped: count,
                    deferred: 0,
                },
            );
            restored_pages += count;
        }
        if let Some((base, extent)) = base_and_extent {
            let total_pages = (span.end - lo) / PAGE;
            let keep = restored_pages + usize::from(!release_slot_from_aperture);
            if total_pages > keep {
                self.release_aperture_pages(base, extent, total_pages - keep);
            }
        }
        Ok(())
    }

    fn promote_faulting_alias(&mut self, fault_page: usize) -> Result<(), u32> {
        let token = *self.mappings.get(&fault_page).ok_or(ERROR_INVALID_ADDRESS)?;
        if !self.secondary_aliases.contains(&fault_page) {
            return Err(ERROR_INVALID_ADDRESS);
        }
        match &self.pages[&token].backing {
            PageBacking::SharedSection => {
                self.secondary_aliases.remove(&fault_page);
                return Ok(());
            }
            PageBacking::Unbacked => {
                shared_pages::protect_private_range(fault_page, PAGE, PAGE_READWRITE)?;
                self.populate_page_bytes(token, fault_page as *mut u8, true)?;
                self.secondary_aliases.remove(&fault_page);
                let is_cpu = self.is_cpu_aperture(fault_page);
                let page = self.pages.get_mut(&token).expect("validated token");
                page.backing = PageBacking::PrivateView(fault_page);
                page.backing_is_cpu = is_cpu;
                page.saved = Vec::new();
                return Ok(());
            }
            PageBacking::PrivateView(_) | PageBacking::PrivateArena { .. } => {}
        }
        let mut all_vas: Vec<usize> = self
            .mappings
            .iter()
            .filter(|&(_, &t)| t == token)
            .map(|(&va, _)| va)
            .collect();
        all_vas.sort_unstable();
        if all_vas.is_empty() {
            return Err(ERROR_INVALID_ADDRESS);
        }
        for &va in &all_vas {
            if let Some((lo, span)) = self.private_span(va)
                && (self.has_hold(lo, span.end)
                    || (lo..span.end).step_by(PAGE).any(|p| self.deferred_unmaps.contains_key(&p)))
            {
                return Err(ERROR_BUSY);
            }
        }
        let mut saved = vec![0u8; PAGE];
        self.populate_page_bytes(token, saved.as_mut_ptr(), true)?;
        {
            let page = &self.pages[&token];
            page.section.write_at(page.offset, saved.as_ptr(), PAGE)?;
        }
        for &va in &all_vas {
            if self.private_span(va).is_some() {
                self.detach_private_slot(va, false)?;
            }
        }
        for &va in &all_vas {
            let info = shared_pages::region(va)?;
            shared_pages::isolate_page(va, &info)?;
            let page = &self.pages[&token];
            page.section.map(page.offset, va)?;
            self.secondary_aliases.remove(&va);
        }
        let page = self.pages.get_mut(&token).expect("token validated");
        page.has_section_data = true;
        page.saved = Vec::new();
        page.backing = PageBacking::SharedSection;
        page.backing_is_cpu = false;
        Ok(())
    }

    fn apply_slot_role(&mut self, at: usize, token: u64, role: SlotRole) {
        self.mappings.insert(at, token);
        match role {
            SlotRole::GpuAlias => {
                self.gpu_aliases.insert(at);
                self.pages.get_mut(&token).expect("validated token").aliases += 1;
            }
            SlotRole::Canonical => {
                let is_cpu = self.is_cpu_aperture(at);
                let page = self.pages.get_mut(&token).expect("validated token");
                page.aliases += 1;
                page.cpu_views += 1;
                page.backing = PageBacking::PrivateView(at);
                page.backing_is_cpu = is_cpu;
                page.saved = Vec::new();
            }
            SlotRole::Upgrade(old_va) => {
                let page = self.pages.get_mut(&token).expect("validated token");
                page.aliases += 1;
                page.cpu_views += 1;
                page.backing = PageBacking::PrivateView(at);
                page.backing_is_cpu = true;
                page.saved = Vec::new();
                if !self.has_hold(old_va, old_va + PAGE) {
                    self.secondary_aliases.insert(old_va);
                }
            }
            SlotRole::SecondaryAlias => {
                let page = self.pages.get_mut(&token).expect("validated token");
                page.aliases += 1;
                page.cpu_views += 1;
                self.secondary_aliases.insert(at);
            }
        }
    }

    pub(super) fn map_private_page(
        &mut self,
        at: usize,
        token: u64,
        gpu_alias: bool,
    ) -> Result<bool, u32> {
        if self.pages.get(&token).ok_or(ERROR_INVALID_PARAMETER)?.mapped {
            return Err(ERROR_BUSY);
        }
        let role = self.classify_private_slot(at, token, gpu_alias);
        let protect = if matches!(role, SlotRole::SecondaryAlias) {
            PAGE_NOACCESS
        } else {
            PAGE_READWRITE
        };
        if let Some((lo, mut span)) = self.private_span(at) {
            if self.mappings.contains_key(&at) {
                return Err(ERROR_INVALID_ADDRESS);
            }
            if matches!(self.deferred_unmaps.get(&at), Some(DeferredKind::PrivatePlaceholder)) {
                shared_pages::protect_private_range(at, PAGE, protect)?;
                if !matches!(role, SlotRole::SecondaryAlias) {
                    self.populate_page_bytes(token, at as *mut u8, true)?;
                }
                self.deferred_unmaps.remove(&at);
                span.deferred -= 1;
            } else {
                shared_pages::commit_private_range_protect(at, PAGE, protect)?;
                if !matches!(role, SlotRole::SecondaryAlias)
                    && let Err(e) = self.populate_page_bytes(token, at as *mut u8, false)
                {
                    let _ = shared_pages::decommit_private_range(at, PAGE);
                    return Err(e);
                }
            }
            if let SlotRole::Upgrade(old_va) = role
                && !self.has_hold(old_va, old_va + PAGE)
            {
                shared_pages::protect_private_range(old_va, PAGE, PAGE_NOACCESS)?;
                ensure_secondary_alias_veh();
            }
            if matches!(role, SlotRole::SecondaryAlias) {
                ensure_secondary_alias_veh();
            }
            span.mapped += 1;
            self.private_views.insert(lo, span);
            self.apply_slot_role(at, token, role);
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
        if let Err(e) = shared_pages::map_private_range_protect(at, PAGE, protect) {
            if existing.is_some() {
                return Err(e);
            }
            if extent == PAGE {
                return Ok(false);
            }
            shared_pages::coalesce(base, extent)?;
            return Err(e);
        }
        if !matches!(role, SlotRole::SecondaryAlias)
            && let Err(e) = self.populate_page_bytes(token, at as *mut u8, false)
        {
            let _ = shared_pages::unmap_private_range(at, PAGE);
            if existing.is_none() {
                let _ = shared_pages::coalesce(base, extent);
            }
            return Err(e);
        }
        if let SlotRole::Upgrade(old_va) = role
            && !self.has_hold(old_va, old_va + PAGE)
        {
            shared_pages::protect_private_range(old_va, PAGE, PAGE_NOACCESS)?;
            ensure_secondary_alias_veh();
        }
        if matches!(role, SlotRole::SecondaryAlias) {
            ensure_secondary_alias_veh();
        }
        self.private_views.insert(
            at,
            PrivateSpan {
                end: at + PAGE,
                mapped: 1,
                deferred: 0,
            },
        );
        self.apertures.entry(base).or_insert((extent, 0)).1 += 1;
        self.apply_slot_role(at, token, role);
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
        let end = start + bytes;
        let mut seen = HashSet::new();
        seen.try_reserve(tokens.len())
            .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
        let mut roles = Vec::new();
        roles
            .try_reserve_exact(tokens.len())
            .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
        for (i, &token) in tokens.iter().enumerate() {
            let p = &self.pages[&token];
            if p.mapped
                || matches!(p.backing, PageBacking::SharedSection)
                || !seen.insert(token)
            {
                return Ok(false);
            }
            roles.push(self.classify_private_slot(start + i * PAGE, token, gpu_alias));
        }
        let all_secondary = roles.iter().all(|r| matches!(r, SlotRole::SecondaryAlias));
        let base_protect = if all_secondary {
            PAGE_NOACCESS
        } else {
            PAGE_READWRITE
        };
        let mut mask_new = Vec::new();
        let mut demote_old = Vec::new();
        for (i, &role) in roles.iter().enumerate() {
            match role {
                SlotRole::SecondaryAlias if !all_secondary => mask_new.push(start + i * PAGE),
                SlotRole::Upgrade(old_va) if !self.has_hold(old_va, old_va + PAGE) => {
                    demote_old.push(old_va);
                }
                _ => {}
            }
        }
        let needs_veh = !gpu_alias
            && (all_secondary || !mask_new.is_empty() || !demote_old.is_empty());

        if let Some((lo, mut span)) = self.private_span(start) {
            if end > span.end || (start..end).step_by(PAGE).any(|va| self.mappings.contains_key(&va)) {
                return Ok(false);
            }
            let mut committed_runs: Vec<(usize, usize)> = Vec::new();
            let mut run_lo: Option<usize> = None;
            let mut deferred_vas = Vec::new();
            for va in (start..end).step_by(PAGE) {
                if self.deferred_unmaps.contains_key(&va) {
                    deferred_vas.push(va);
                    if let Some(r_lo) = run_lo.take() {
                        committed_runs.push((r_lo, va - r_lo));
                    }
                } else {
                    run_lo.get_or_insert(va);
                }
            }
            if let Some(r_lo) = run_lo {
                committed_runs.push((r_lo, end - r_lo));
            }
            for (done, &(r_lo, r_len)) in committed_runs.iter().enumerate() {
                if let Err(e) = shared_pages::commit_private_range_protect(r_lo, r_len, base_protect) {
                    for &(p_lo, p_len) in &committed_runs[..done] {
                        let _ = shared_pages::decommit_private_range(p_lo, p_len);
                    }
                    return Err(e);
                }
            }
            if !deferred_vas.is_empty() {
                let mut def_copy = deferred_vas.clone();
                protect_runs(&mut def_copy, base_protect)?;
            }
            if !all_secondary {
                for (i, &token) in tokens.iter().enumerate() {
                    if matches!(roles[i], SlotRole::SecondaryAlias) {
                        continue;
                    }
                    let va = start + i * PAGE;
                    let zero = self.deferred_unmaps.contains_key(&va);
                    if let Err(e) = self.populate_page_bytes(token, va as *mut u8, zero) {
                        for &(r_lo, r_len) in &committed_runs {
                            let _ = shared_pages::decommit_private_range(r_lo, r_len);
                        }
                        return Err(e);
                    }
                }
            }
            if !mask_new.is_empty() {
                protect_runs(&mut mask_new, PAGE_NOACCESS)?;
            }
            if !demote_old.is_empty() {
                protect_runs(&mut demote_old, PAGE_NOACCESS)?;
            }
            if needs_veh {
                ensure_secondary_alias_veh();
            }
            for &va in &deferred_vas {
                self.deferred_unmaps.remove(&va);
            }
            span.deferred -= deferred_vas.len();
            span.mapped += tokens.len();
            self.private_views.insert(lo, span);
            for (i, &token) in tokens.iter().enumerate() {
                self.apply_slot_role(start + i * PAGE, token, roles[i]);
            }
            return Ok(true);
        }

        if self.private_views.range(start..end).next().is_some()
            || (start..end)
                .step_by(PAGE)
                .any(|va| self.deferred_unmaps.contains_key(&va))
        {
            return Ok(false);
        }
        let info = shared_pages::region(start)?;
        if info.State != MEM_RESERVE
            || info.AllocationProtect != PAGE_NOACCESS
            || end > info.BaseAddress.addr() + info.RegionSize
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
        if let Err(e) = shared_pages::map_private_range_protect(start, bytes, base_protect) {
            if self.aperture(start).is_none() && start == base && bytes == extent {
                return Ok(false);
            }
            if self.aperture(start).is_none() {
                shared_pages::coalesce(base, extent)?;
            }
            return Err(e);
        }
        if !all_secondary {
            for (i, &token) in tokens.iter().enumerate() {
                if matches!(roles[i], SlotRole::SecondaryAlias) {
                    continue;
                }
                if let Err(e) = self.populate_page_bytes(token, (start + i * PAGE) as *mut u8, false) {
                    let _ = shared_pages::unmap_private_range(start, bytes);
                    if self.aperture(start).is_none() {
                        let _ = shared_pages::coalesce(base, extent);
                    }
                    return Err(e);
                }
            }
        }
        if !mask_new.is_empty() {
            protect_runs(&mut mask_new, PAGE_NOACCESS)?;
        }
        if !demote_old.is_empty() {
            protect_runs(&mut demote_old, PAGE_NOACCESS)?;
        }
        if needs_veh {
            ensure_secondary_alias_veh();
        }
        self.private_views.insert(
            start,
            PrivateSpan {
                end,
                mapped: tokens.len(),
                deferred: 0,
            },
        );
        self.apertures.entry(base).or_insert((extent, 0)).1 += tokens.len();
        for (i, &token) in tokens.iter().enumerate() {
            self.apply_slot_role(start + i * PAGE, token, roles[i]);
        }
        Ok(true)
    }

    pub(super) fn unmap_private_view_range(
        &mut self,
        at: usize,
        bytes: usize,
        save_bytes: bool,
    ) -> Result<bool, u32> {
        let Some((lo, mut span)) = self.private_span(at) else {
            return Ok(false);
        };
        let end = at + bytes;
        if end > span.end {
            return Ok(false);
        }
        let (base, extent) = self.aperture(at).ok_or(ERROR_INVALID_ADDRESS)?;
        if (at..end).step_by(PAGE).any(|va| !self.mappings.contains_key(&va)) {
            return Err(ERROR_INVALID_ADDRESS);
        }
        let count = bytes / PAGE;
        let remaining = (span.mapped - count) + span.deferred;
        if remaining == 0 && !self.has_hold(lo, span.end) {
            for va in (at..end).step_by(PAGE) {
                self.clear_page_mapping(va, save_bytes);
            }
            shared_pages::unmap_private_range(lo, span.end - lo)?;
            self.private_views.remove(&lo);
            self.release_aperture_pages(base, extent, (span.end - lo) / PAGE);
            return Ok(true);
        }
        let mut decommit_runs: Vec<(usize, usize)> = Vec::new();
        let mut run_lo: Option<usize> = None;
        let mut deferred_added = 0usize;
        for va in (at..end).step_by(PAGE) {
            self.clear_page_mapping(va, save_bytes);
            if self.has_hold(va, va + PAGE) {
                self.deferred_unmaps.insert(va, DeferredKind::PrivatePlaceholder);
                deferred_added += 1;
                if let Some(r_lo) = run_lo.take() {
                    decommit_runs.push((r_lo, va - r_lo));
                }
            } else {
                run_lo.get_or_insert(va);
            }
        }
        if let Some(r_lo) = run_lo {
            decommit_runs.push((r_lo, end - r_lo));
        }
        for (r_lo, r_len) in decommit_runs {
            shared_pages::decommit_private_range(r_lo, r_len)?;
        }
        span.mapped -= count;
        span.deferred += deferred_added;
        self.private_views.insert(lo, span);
        Ok(true)
    }
}
