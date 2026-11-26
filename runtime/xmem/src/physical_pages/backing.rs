//! Private anonymous physical backings and lifetime holds for zero-copy GPU imports (#191).
use super::{PAGE, POOL, Pool, shared_pages};
use core::ptr;
use std::collections::{HashMap, HashSet};
use std::sync::Arc;
use windows_sys::Win32::Foundation::{
    ERROR_INVALID_ADDRESS, ERROR_INVALID_PARAMETER, ERROR_MORE_DATA, ERROR_NOT_ENOUGH_MEMORY,
    ERROR_NOT_FOUND, ERROR_NOT_SUPPORTED, GetLastError, SetLastError,
};
use windows_sys::Win32::System::Memory::{
    MEM_COMMIT, MEM_DECOMMIT, MEM_RELEASE, MEM_RESERVE, PAGE_READWRITE, VirtualAlloc, VirtualFree,
};

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct BackingRun {
    pub address: u64,
    pub bytes: u64,
}

/// Owned private anonymous allocation backing GPU-only alias pages when no
/// live CPU view covers them.
pub(super) struct PrivateArena {
    base: usize,
}

impl PrivateArena {
    pub(super) fn new(bytes: usize) -> Result<Arc<Self>, u32> {
        if bytes == 0 || !bytes.is_multiple_of(PAGE) {
            return Err(ERROR_INVALID_PARAMETER);
        }
        // SAFETY: `ptr::null()` asks the OS for a private anonymous region.
        let base = unsafe { VirtualAlloc(ptr::null(), bytes, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) };
        if base.is_null() {
            return Err(match unsafe { GetLastError() } {
                0 => ERROR_NOT_ENOUGH_MEMORY,
                error => error,
            });
        }
        Ok(Arc::new(Self { base: base.addr() }))
    }

    pub(super) fn base(&self) -> usize {
        self.base
    }
}

impl Drop for PrivateArena {
    fn drop(&mut self) {
        if self.base != 0 {
            // SAFETY: `self.base` was committed via `VirtualAlloc(MEM_RESERVE | MEM_COMMIT)` above.
            let _ = unsafe { VirtualFree(self.base as _, 0, MEM_RELEASE) };
        }
    }
}

#[derive(Clone, Default)]
pub(super) enum PageBacking {
    #[default]
    Unbacked,
    PrivateView(usize),
    PrivateArena {
        arena: Arc<PrivateArena>,
        offset: usize,
    },
    SharedSection,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) enum DeferredKind {
    PrivatePlaceholder,
    SectionPlaceholder,
    LegacyCommit,
}

pub(super) struct HoldRecord {
    pub(super) arenas: Vec<Arc<PrivateArena>>,
    pub(super) pinned_views: Vec<usize>,
}

fn failure(code: u32) -> i32 {
    unsafe { SetLastError(code) };
    0
}

impl Pool {
    pub(super) fn has_hold(&self, start: usize, end: usize) -> bool {
        (start..end)
            .step_by(PAGE)
            .any(|va| self.view_holds.get(&va).copied().unwrap_or(0) > 0)
    }

    /// Resolve a 64 KiB-aligned guest range into canonical `MEM_PRIVATE` host
    /// runs (promoting GPU-alias-only unbacked tokens into a private arena once)
    /// and a `HoldRecord` that keeps every backing live until released.
    pub(super) fn resolve_backings(
        &mut self,
        address: usize,
        bytes: usize,
    ) -> Result<(Vec<BackingRun>, HoldRecord), u32> {
        let Some(end) = address.checked_add(bytes) else {
            return Err(ERROR_INVALID_PARAMETER);
        };
        if address == 0 || bytes == 0 || !address.is_multiple_of(PAGE) || !bytes.is_multiple_of(PAGE) {
            return Err(ERROR_INVALID_PARAMETER);
        }
        let count = bytes / PAGE;
        let mut guest_tokens = Vec::new();
        guest_tokens
            .try_reserve_exact(count)
            .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
        let mut mapped_count = 0usize;
        let mut unmapped_count = 0usize;
        for va in (address..end).step_by(PAGE) {
            match self.mappings.get(&va).copied() {
                Some(token) => {
                    mapped_count += 1;
                    guest_tokens.push((va, token));
                }
                None => unmapped_count += 1,
            }
        }
        if mapped_count == 0 {
            return Err(ERROR_NOT_FOUND);
        }
        if unmapped_count != 0 {
            return Err(ERROR_INVALID_ADDRESS);
        }

        let mut seen_unbacked = HashSet::new();
        let mut unbacked = Vec::new();
        for &(_, token) in &guest_tokens {
            let page = &self.pages[&token];
            if matches!(page.backing, PageBacking::Unbacked) && seen_unbacked.insert(token) {
                unbacked.push(token);
            }
        }

        if !unbacked.is_empty() {
            let arena = PrivateArena::new(unbacked.len() * PAGE)?;
            for (slot, &token) in unbacked.iter().enumerate() {
                let page = &self.pages[&token];
                let dst = (arena.base() + slot * PAGE) as *mut u8;
                if !page.saved.is_empty() {
                    // SAFETY: `dst` points into a freshly committed `PAGE`-byte arena slot.
                    unsafe { ptr::copy_nonoverlapping(page.saved.as_ptr(), dst, PAGE) };
                } else if page.has_section_data {
                    page.section.read_into(page.offset, dst)?;
                }
            }
            for (slot, &token) in unbacked.iter().enumerate() {
                let page = self.pages.get_mut(&token).expect("mapped token exists");
                page.saved = Vec::new();
                page.backing = PageBacking::PrivateArena {
                    arena: Arc::clone(&arena),
                    offset: slot * PAGE,
                };
            }
        }

        let mut runs: Vec<BackingRun> = Vec::new();
        let mut pinned_views = Vec::new();
        let mut pinned_set = HashSet::new();
        let mut arenas: Vec<Arc<PrivateArena>> = Vec::new();
        let mut arena_set = HashSet::new();
        for &(va, token) in &guest_tokens {
            let host = match &self.pages[&token].backing {
                PageBacking::PrivateView(view_va) => {
                    if pinned_set.insert(*view_va) {
                        pinned_views.push(*view_va);
                    }
                    *view_va as u64
                }
                PageBacking::PrivateArena { arena, offset } => {
                    if arena_set.insert(Arc::as_ptr(arena) as usize) {
                        arenas.push(Arc::clone(arena));
                    }
                    (arena.base() + *offset) as u64
                }
                PageBacking::SharedSection => {
                    if pinned_set.insert(va) {
                        pinned_views.push(va);
                    }
                    va as u64
                }
                PageBacking::Unbacked => unreachable!("all unbacked tokens received a PrivateArena"),
            };
            if let Some(last) = runs.last_mut()
                && last.address + last.bytes == host
            {
                last.bytes += PAGE as u64;
            } else {
                runs.push(BackingRun { address: host, bytes: PAGE as u64 });
            }
        }
        Ok((runs, HoldRecord { arenas, pinned_views }))
    }

    pub(super) fn commit_hold(&mut self, record: HoldRecord) -> Result<u64, u32> {
        self.next_hold += 1;
        let id = self.next_hold;
        self.holds
            .try_reserve(1)
            .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
        self.view_holds
            .try_reserve(record.pinned_views.len())
            .map_err(|_| ERROR_NOT_ENOUGH_MEMORY)?;
        for &va in &record.pinned_views {
            *self.view_holds.entry(va).or_insert(0) += 1;
        }
        self.holds.insert(id, record);
        Ok(id)
    }

    pub(super) fn release_hold(&mut self, id: u64) -> Option<Vec<Arc<PrivateArena>>> {
        let record = self.holds.remove(&id)?;
        let mut released_vas = Vec::new();
        for &va in &record.pinned_views {
            if let Some(count) = self.view_holds.get_mut(&va) {
                *count -= 1;
                if *count == 0 {
                    self.view_holds.remove(&va);
                    released_vas.push(va);
                }
            }
        }
        self.flush_deferred_unmaps(&released_vas);
        Some(record.arenas)
    }

    fn flush_deferred_unmaps(&mut self, released_vas: &[usize]) {
        if self.deferred_unmaps.is_empty() || released_vas.is_empty() {
            return;
        }
        let mut spans_to_clean: HashMap<usize, usize> = HashMap::new();
        let mut section_spans_to_clean: HashMap<usize, usize> = HashMap::new();
        let mut legacy_or_single = Vec::new();
        for &va in released_vas {
            let Some(&kind) = self.deferred_unmaps.get(&va) else {
                continue;
            };
            match kind {
                DeferredKind::LegacyCommit => legacy_or_single.push((va, kind)),
                DeferredKind::SectionPlaceholder => {
                    if let Some((&lo, &hi)) = self.views.range(..=va).next_back()
                        && va < hi
                    {
                        section_spans_to_clean.insert(lo, hi);
                    } else {
                        legacy_or_single.push((va, kind));
                    }
                }
                DeferredKind::PrivatePlaceholder => {
                    if let Some((&lo, &hi)) = self.private_views.range(..=va).next_back()
                        && va < hi
                    {
                        spans_to_clean.insert(lo, hi);
                    }
                }
            }
        }
        for (va, kind) in legacy_or_single {
            if self.view_holds.contains_key(&va) {
                continue;
            }
            match kind {
                DeferredKind::LegacyCommit => {
                    if unsafe { VirtualFree(va as _, PAGE, MEM_DECOMMIT) } != 0 {
                        self.deferred_unmaps.remove(&va);
                    }
                }
                DeferredKind::SectionPlaceholder => {
                    if let Some((base, extent)) = self.aperture(va)
                        && shared_pages::unmap(va).is_ok()
                    {
                        self.deferred_unmaps.remove(&va);
                        self.release_aperture_pages(base, extent, 1);
                    }
                }
                DeferredKind::PrivatePlaceholder => {}
            }
        }
        for (lo, hi) in section_spans_to_clean {
            if self.has_hold(lo, hi) {
                continue;
            }
            self.reconcile_deferred_section_span(lo, hi);
        }
        for (lo, hi) in spans_to_clean {
            if self.has_hold(lo, hi) {
                continue;
            }
            self.reconcile_deferred_private_span(lo, hi);
        }
    }

    pub(super) fn release_aperture_pages(&mut self, base: usize, extent: usize, count: usize) {
        if let Some(entry) = self.apertures.get_mut(&base) {
            if entry.1 <= count {
                if shared_pages::coalesce(base, extent).is_ok() {
                    self.apertures.remove(&base);
                } else {
                    entry.1 = 0;
                }
            } else {
                entry.1 -= count;
            }
        }
    }

    fn reconcile_deferred_private_span(&mut self, lo: usize, hi: usize) {
        let Some((base, extent)) = self.aperture(lo) else {
            return;
        };
        let live_runs = self.collect_live_runs(lo, hi);
        if live_runs.is_empty() {
            if shared_pages::unmap_private_range(lo, hi - lo).is_ok() {
                for va in (lo..hi).step_by(PAGE) {
                    self.deferred_unmaps.remove(&va);
                }
                self.private_views.remove(&lo);
                self.release_aperture_pages(base, extent, (hi - lo) / PAGE);
            }
            return;
        }
        let mut snapshots = Vec::new();
        for &(r_lo, r_hi) in &live_runs {
            let len = r_hi - r_lo;
            let mut buf = vec![0u8; len];
            // SAFETY: every page in `r_lo..r_hi` is currently committed `PAGE_READWRITE`.
            unsafe { ptr::copy_nonoverlapping(r_lo as *const u8, buf.as_mut_ptr(), len) };
            snapshots.push((r_lo, r_hi, buf));
        }
        if shared_pages::unmap_private_range(lo, hi - lo).is_err() {
            return;
        }
        for va in (lo..hi).step_by(PAGE) {
            self.deferred_unmaps.remove(&va);
        }
        self.private_views.remove(&lo);
        let mut restored_pages = 0usize;
        for (r_lo, r_hi, buf) in snapshots {
            if let Ok(info) = shared_pages::region(r_lo)
                && shared_pages::isolate_range(r_lo, r_hi - r_lo, &info).is_ok()
                && shared_pages::map_private_range(r_lo, r_hi - r_lo).is_ok()
            {
                unsafe { ptr::copy_nonoverlapping(buf.as_ptr(), r_lo as *mut u8, r_hi - r_lo) };
                self.private_views.insert(r_lo, r_hi);
                restored_pages += (r_hi - r_lo) / PAGE;
            }
        }
        let dropped = (hi - lo) / PAGE - restored_pages;
        if dropped > 0 {
            self.release_aperture_pages(base, extent, dropped);
        }
    }

    fn reconcile_deferred_section_span(&mut self, lo: usize, hi: usize) {
        let Some((base, extent)) = self.aperture(lo) else {
            return;
        };
        let live_runs = self.collect_live_runs(lo, hi);
        let Some(&first_token) = live_runs
            .first()
            .and_then(|&(a, _)| self.mappings.get(&a))
            .or_else(|| None)
        else {
            if shared_pages::unmap(lo).is_ok() {
                for va in (lo..hi).step_by(PAGE) {
                    self.deferred_unmaps.remove(&va);
                }
                self.views.remove(&lo);
                self.release_aperture_pages(base, extent, (hi - lo) / PAGE);
            }
            return;
        };
        let section = Arc::clone(&self.pages[&first_token].section);
        let first_offset = self.pages[&first_token].offset;
        let run_base_va = live_runs[0].0;
        if shared_pages::unmap(lo).is_err() {
            return;
        }
        for va in (lo..hi).step_by(PAGE) {
            self.deferred_unmaps.remove(&va);
        }
        self.views.remove(&lo);
        let mut restored_pages = 0usize;
        for &(r_lo, r_hi) in &live_runs {
            let offset = first_offset + (r_lo as isize - run_base_va as isize) as u64;
            if let Ok(info) = shared_pages::region(r_lo)
                && shared_pages::isolate_range(r_lo, r_hi - r_lo, &info).is_ok()
                && section.map_range(offset, r_lo, r_hi - r_lo).is_ok()
            {
                self.views.insert(r_lo, r_hi);
                restored_pages += (r_hi - r_lo) / PAGE;
            }
        }
        let dropped = (hi - lo) / PAGE - restored_pages;
        if dropped > 0 {
            self.release_aperture_pages(base, extent, dropped);
        }
    }

    fn collect_live_runs(&self, lo: usize, hi: usize) -> Vec<(usize, usize)> {
        let mut runs = Vec::new();
        let mut cur_start: Option<usize> = None;
        for va in (lo..hi).step_by(PAGE) {
            if !self.deferred_unmaps.contains_key(&va) && self.mappings.contains_key(&va) {
                cur_start.get_or_insert(va);
            } else if let Some(start) = cur_start.take() {
                runs.push((start, va));
            }
        }
        if let Some(start) = cur_start {
            runs.push((start, hi));
        }
        runs
    }
}

/// Resolve a 64 KiB-aligned guest range to one contiguous `MEM_PRIVATE` host
/// range and pin its backing until `TuxBoxReleasePhysicalBacking(hold)`.
///
/// # Safety
/// `backing` and `hold` must be writable 8-byte aligned outputs.
#[unsafe(export_name = "TuxBoxAcquirePhysicalBacking")]
pub unsafe extern "system" fn acquire_physical_backing(
    address: u64,
    bytes: u64,
    backing: *mut u64,
    hold: *mut u64,
) -> i32 {
    if backing.is_null() || hold.is_null() {
        return failure(ERROR_INVALID_PARAMETER);
    }
    let (Ok(addr), Ok(len)) = (usize::try_from(address), usize::try_from(bytes)) else {
        return failure(ERROR_INVALID_PARAMETER);
    };
    let mut pool = POOL.lock().unwrap_or_else(|e| e.into_inner());
    let (runs, record) = match pool.resolve_backings(addr, len) {
        Ok(ok) => ok,
        Err(error) => return failure(error),
    };
    if runs.len() != 1 {
        return failure(ERROR_NOT_SUPPORTED);
    }
    let cookie = match pool.commit_hold(record) {
        Ok(cookie) => cookie,
        Err(error) => return failure(error),
    };
    unsafe {
        backing.write_unaligned(runs[0].address);
        hold.write_unaligned(cookie);
    }
    1
}

/// Resolve a 64 KiB-aligned guest range to an ordered slice of 64 KiB-aligned
/// `MEM_PRIVATE` host runs and pin all backings under a single `hold`.
///
/// # Safety
/// `count` and `hold` must be writable; `runs` must hold `*count` entries on
/// entry when non-null.
#[unsafe(export_name = "TuxBoxAcquirePhysicalBackingRuns")]
pub unsafe extern "system" fn acquire_physical_backing_runs(
    address: u64,
    bytes: u64,
    runs: *mut BackingRun,
    count: *mut u32,
    hold: *mut u64,
) -> i32 {
    if count.is_null() || hold.is_null() {
        return failure(ERROR_INVALID_PARAMETER);
    }
    let (Ok(addr), Ok(len)) = (usize::try_from(address), usize::try_from(bytes)) else {
        return failure(ERROR_INVALID_PARAMETER);
    };
    let mut pool = POOL.lock().unwrap_or_else(|e| e.into_inner());
    let (resolved, record) = match pool.resolve_backings(addr, len) {
        Ok(ok) => ok,
        Err(error) => return failure(error),
    };
    let needed = match u32::try_from(resolved.len()) {
        Ok(n) => n,
        Err(_) => return failure(ERROR_INVALID_PARAMETER),
    };
    let capacity = unsafe { count.read_unaligned() } as usize;
    unsafe { count.write_unaligned(needed) };
    if capacity < resolved.len() || runs.is_null() {
        return failure(ERROR_MORE_DATA);
    }
    let cookie = match pool.commit_hold(record) {
        Ok(cookie) => cookie,
        Err(error) => return failure(error),
    };
    for (i, run) in resolved.iter().enumerate() {
        unsafe { runs.add(i).write_unaligned(*run) };
    }
    unsafe { hold.write_unaligned(cookie) };
    1
}

/// Release a hold obtained from `TuxBoxAcquirePhysicalBacking` or
/// `TuxBoxAcquirePhysicalBackingRuns`.
#[unsafe(export_name = "TuxBoxReleasePhysicalBacking")]
pub extern "system" fn release_physical_backing(hold: u64) {
    if hold == 0 {
        return;
    }
    let arenas = {
        let mut pool = POOL.lock().unwrap_or_else(|e| e.into_inner());
        pool.release_hold(hold)
    };
    drop(arenas);
}

#[cfg(test)]
#[path = "../tests/physical_backing.rs"]
mod tests;
