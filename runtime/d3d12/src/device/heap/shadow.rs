//! A copy of game memory the GPU can read, for hosts whose imports of the game's
//! section views do not reach the real pages (see the host profile).
//!
//! CPU writes are copied in before each submission. GPU writes stay in the copy:
//! the CPU and other addresses of the same pages do not see them.
//!
//! From pull request #200 by DrArroway.
use super::{ImportedHeap, trace_import_failure};
use crate::commands::page_mapping::ImportPin;
use core::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, LazyLock, Mutex, Weak};
use windows::Win32::Foundation::{E_INVALIDARG, E_OUTOFMEMORY};
use windows::Win32::Graphics::Direct3D12::{ID3D12Device3, ID3D12Device13};
use windows::Win32::System::Memory::{
    MEM_COMMIT, MEM_MAPPED, MEM_RELEASE, MEM_RESERVE, MEMORY_BASIC_INFORMATION, PAGE_GUARD,
    PAGE_NOACCESS, PAGE_READWRITE, VirtualAlloc, VirtualFree, VirtualQuery,
};
use windows::core::Result;

const MAX_SHADOW_BYTES: usize = 1 << 30;
const PAGE_STEP: usize = 4096;
const CHUNK_BYTES: usize = 64;

struct ShadowState {
    baseline: Vec<u8>,
    retired: bool,
}

/// Private anonymous host copy of committed section-backed guest pages.
/// On drivers such as RADV that restrict `VK_EXT_external_memory_host` to
/// anonymous userptr pages, `OpenExistingHeapFromAddress*` imports this buffer
/// while CPU modifications in non-retired pages are mirrored before submit.
pub(super) struct ShadowPages {
    guest_base: u64,
    shadow_base: usize,
    size: usize,
    pin_live: Arc<AtomicBool>,
    state: Mutex<ShadowState>,
}

struct AllocGuard(usize);
impl Drop for AllocGuard {
    fn drop(&mut self) {
        if self.0 != 0 {
            let _ = unsafe { VirtualFree(self.0 as _, 0, MEM_RELEASE) };
        }
    }
}

impl Drop for ShadowPages {
    fn drop(&mut self) {
        if self.shadow_base != 0 {
            // SAFETY: `shadow_base` was allocated by `VirtualAlloc(MEM_RESERVE | MEM_COMMIT)`
            // and outlives `ImportedHeap::native` (declared earlier in `ImportedHeap`).
            let _ = unsafe { VirtualFree(self.shadow_base as _, 0, MEM_RELEASE) };
        }
    }
}

impl ShadowPages {
    fn new(guest_base: u64, size: usize, pin_live: Arc<AtomicBool>) -> Result<Arc<Self>> {
        if size == 0 || size > MAX_SHADOW_BYTES {
            return Err(E_INVALIDARG.into());
        }
        let mut baseline = Vec::new();
        baseline
            .try_reserve_exact(size)
            .map_err(|_| E_OUTOFMEMORY)?;
        // SAFETY: `None` base asks the OS for an anonymous private region of `size` bytes.
        let shadow_ptr = unsafe { VirtualAlloc(None, size, MEM_RESERVE | MEM_COMMIT, PAGE_READWRITE) };
        if shadow_ptr.is_null() {
            return Err(E_OUTOFMEMORY.into());
        }
        let mut guard = AllocGuard(shadow_ptr.addr());
        if !is_guest_readable(guest_base, size) {
            return Err(E_INVALIDARG.into());
        }
        // SAFETY: `is_guest_readable` confirmed `guest_base..guest_base + size` is committed and
        // readable, and `shadow_ptr` holds `size` writable bytes.
        let guest = unsafe { core::slice::from_raw_parts(guest_base as *const u8, size) };
        baseline.extend_from_slice(guest);
        unsafe { core::ptr::copy_nonoverlapping(baseline.as_ptr(), shadow_ptr.cast(), size) };
        let shadow_base = guard.0;
        guard.0 = 0;
        Ok(Arc::new(Self {
            guest_base,
            shadow_base,
            size,
            pin_live,
            state: Mutex::new(ShadowState {
                baseline,
                retired: false,
            }),
        }))
    }

    fn ptr(&self) -> *const c_void {
        self.shadow_base as _
    }

    fn is_retired(&self) -> bool {
        self.state.lock().unwrap_or_else(|e| e.into_inner()).retired
    }

    /// Copy CPU-modified 64-byte chunks into the anonymous shadow buffer using
    /// a fast 4 KiB page compare first. Returns `false` once retired by a remap.
    fn sync_cpu_writes(&self) -> bool {
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        if state.retired {
            return false;
        }
        if !self.pin_live.load(Ordering::Acquire) {
            state.retired = true;
            return false;
        }
        if !is_guest_readable(self.guest_base, self.size) {
            return true;
        }
        // SAFETY: `is_guest_readable` checked commit + protection on `guest_base..+size`,
        // and `shadow_base` owns `size` RW bytes guarded by `self.state`.
        let guest = unsafe { core::slice::from_raw_parts(self.guest_base as *const u8, self.size) };
        let shadow = unsafe { core::slice::from_raw_parts_mut(self.shadow_base as *mut u8, self.size) };
        diff_copy_chunks(guest, &mut state.baseline, shadow);
        true
    }
}

/// True when `base..base+size` is fully committed `PAGE_READWRITE` and contains
/// at least one section-backed (`MEM_MAPPED`) sub-region.
pub(super) fn contains_mapped_pages(base: u64, size: u64) -> bool {
    let Some(end) = base.checked_add(size) else {
        return false;
    };
    if size == 0 {
        return false;
    }
    let mut cursor = base;
    let mut saw_mapped = false;
    while cursor < end {
        let mut region = MEMORY_BASIC_INFORMATION::default();
        // SAFETY: `VirtualQuery` only inspects virtual memory attributes.
        if unsafe { VirtualQuery(Some(cursor as _), &mut region, size_of_val(&region)) } == 0
            || region.State != MEM_COMMIT
            || region.Protect != PAGE_READWRITE
        {
            return false;
        }
        if region.Type == MEM_MAPPED {
            saw_mapped = true;
        }
        let Some(next) = (region.BaseAddress as u64).checked_add(region.RegionSize as u64) else {
            return false;
        };
        if next <= cursor {
            return false;
        }
        cursor = next;
    }
    saw_mapped
}

fn is_guest_readable(address: u64, size: usize) -> bool {
    let Some(end) = address.checked_add(size as u64) else {
        return false;
    };
    if size == 0 {
        return true;
    }
    let mut cursor = address;
    while cursor < end {
        let mut info = MEMORY_BASIC_INFORMATION::default();
        // SAFETY: `VirtualQuery` only inspects the process memory map.
        let queried = unsafe { VirtualQuery(Some(cursor as _), &mut info, size_of_val(&info)) };
        let protect = info.Protect.0;
        if queried == 0
            || info.State != MEM_COMMIT
            || protect & PAGE_GUARD.0 != 0
            || protect & 0xff == PAGE_NOACCESS.0
        {
            return false;
        }
        let Some(next) = (info.BaseAddress as u64).checked_add(info.RegionSize as u64) else {
            return false;
        };
        if next <= cursor {
            return false;
        }
        cursor = next;
    }
    true
}

/// Skip unchanged 4 KiB pages, then sync only 64-byte chunks where CPU memory
/// diverged from `baseline`, leaving any GPU-written chunks in `shadow` intact.
fn diff_copy_chunks(guest: &[u8], baseline: &mut [u8], shadow: &mut [u8]) -> usize {
    let mut copied = 0usize;
    for ((gp, bp), sp) in guest
        .chunks(PAGE_STEP)
        .zip(baseline.chunks_mut(PAGE_STEP))
        .zip(shadow.chunks_mut(PAGE_STEP))
    {
        if gp == &*bp {
            continue;
        }
        for ((g, b), s) in gp
            .chunks(CHUNK_BYTES)
            .zip(bp.chunks_mut(CHUNK_BYTES))
            .zip(sp.chunks_mut(CHUNK_BYTES))
        {
            if g != &*b {
                s.copy_from_slice(g);
                b.copy_from_slice(g);
                copied += g.len();
            }
        }
    }
    copied
}

static SHADOWS: LazyLock<Mutex<Vec<Weak<ShadowPages>>>> = LazyLock::new(Default::default);
static SHADOW_COUNT: AtomicU32 = AtomicU32::new(0);

fn register(shadow: &Arc<ShadowPages>) {
    let mut shadows = SHADOWS.lock().unwrap_or_else(|e| e.into_inner());
    shadows.retain(|weak| weak.strong_count() != 0);
    shadows.push(Arc::downgrade(shadow));
    SHADOW_COUNT.store(shadows.len().min(u32::MAX as usize) as u32, Ordering::Release);
}

/// Synchronize CPU writes into all active anonymous shadow heaps before GPU
/// command-list submission. Zero-lock fast path when no shadows exist.
pub(crate) fn sync_shadow_imports() {
    if SHADOW_COUNT.load(Ordering::Acquire) == 0 {
        return;
    }
    let sync_live = |live: &mut Vec<Arc<ShadowPages>>| {
        live.clear();
        {
            let mut shadows = SHADOWS.lock().unwrap_or_else(|e| e.into_inner());
            shadows.retain(|weak| weak.strong_count() != 0);
            live.extend(shadows.iter().filter_map(Weak::upgrade));
            SHADOW_COUNT.store(shadows.len().min(u32::MAX as usize) as u32, Ordering::Release);
        }
        let mut any_retired = false;
        for shadow in live.iter() {
            if !shadow.sync_cpu_writes() {
                any_retired = true;
            }
        }
        live.clear();
        if live.capacity() > 4096 {
            live.shrink_to_fit();
        }
        if any_retired {
            let mut shadows = SHADOWS.lock().unwrap_or_else(|e| e.into_inner());
            shadows.retain(|weak| weak.upgrade().is_some_and(|s| !s.is_retired()));
            SHADOW_COUNT.store(shadows.len().min(u32::MAX as usize) as u32, Ordering::Release);
        }
    };
    std::thread_local! {
        static LIVE: std::cell::RefCell<Vec<Arc<ShadowPages>>> = const { std::cell::RefCell::new(Vec::new()) };
    }
    LIVE.with(|cell| {
        if let Ok(mut live) = cell.try_borrow_mut() {
            sync_live(&mut live);
        } else {
            let mut fallback = Vec::new();
            sync_live(&mut fallback);
        }
    });
}

fn log_first_mirror() {
    static LOGGED: std::sync::Once = std::sync::Once::new();
    crate::diagnostics::log_once(&LOGGED, || {
        "game memory is copied for the GPU: this host cannot import it directly".into()
    });
}

pub(super) fn open_shadow_sized(
    device: &ID3D12Device13,
    backing: super::backing::Backing,
    size: u64,
    pin: ImportPin,
    source: &'static str,
) -> Result<Arc<ImportedHeap>> {
    let len = usize::try_from(size).map_err(|_| E_INVALIDARG)?;
    let shadow = ShadowPages::new(backing.address, len, pin.live_flag())?;
    let native = unsafe { device.OpenExistingHeapFromAddress1(shadow.ptr(), shadow.size) }.map_err(|e| {
        trace_import_failure(source, "sized-shadow", size, e.code());
        e
    })?;
    register(&shadow);
    log_first_mirror();
    Ok(Arc::new(ImportedHeap::shadowed(native, pin, shadow, backing)?))
}

pub(super) fn open_shadow_legacy(
    host: &ID3D12Device3,
    backing: super::backing::Backing,
    size: u64,
    pin: ImportPin,
    source: &'static str,
) -> Result<Arc<ImportedHeap>> {
    let len = usize::try_from(size).map_err(|_| E_INVALIDARG)?;
    let shadow = ShadowPages::new(backing.address, len, pin.live_flag())?;
    let native = unsafe { host.OpenExistingHeapFromAddress(shadow.ptr()) }.map_err(|e| {
        trace_import_failure(source, "legacy-shadow", size, e.code());
        e
    })?;
    register(&shadow);
    log_first_mirror();
    Ok(Arc::new(ImportedHeap::shadowed(native, pin, shadow, backing)?))
}

#[cfg(test)]
#[path = "../../tests/device/heap_shadow.rs"]
mod tests;
