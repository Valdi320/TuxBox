//! The host memory behind a range of XMem physical pages.
//!
//! amdgpu imports only private anonymous memory as a host pointer (#191).
//! XMem can give each physical page one private backing and leave its other
//! addresses as GPU-only aliases. A GPU import of an alias must then use the
//! backing, so that every alias and the CPU see the same memory.
//!
//! The backing must also stay mapped for as long as an import of it exists.
//! RADV passes every live buffer with each submission, so one import whose
//! host pages are gone makes the kernel refuse every later submission with
//! `EFAULT`, and the device is lost. The importer therefore holds the backing
//! until its native heap is released.
//!
//! XMem provides both through two exports:
//!
//! ```text
//! BOOL TuxBoxAcquirePhysicalBacking(UINT64 address, UINT64 bytes, UINT64* backing, UINT64* hold);
//! void TuxBoxReleasePhysicalBacking(UINT64 hold);
//! ```
//!
//! For a 64 KiB-aligned range, `TuxBoxAcquirePhysicalBacking` writes the start
//! of the contiguous host range that holds the same bytes, which may equal
//! `address`, and a hold. When a range spans pages whose backings are not
//! contiguous, `TuxBoxAcquirePhysicalBackingRuns` (when exported) returns one
//! hold covering an ordered list of 64 KiB-aligned piece runs that can be bound
//! together via a reserved buffer and `UpdateTileMappings`. While a hold lives,
//! XMem keeps those host ranges mapped to private memory, even if the title
//! unmaps, remaps or frees the pages. What it contains after a remap does not
//! matter: the queue retires imports of remapped pages. Acquire fails with
//! `ERROR_NOT_FOUND` when the range holds no XMem physical page; the importer
//! then uses the address itself, as it does without the exports. Any other
//! failure fails the import.
use std::sync::OnceLock;
use windows::Win32::Foundation::{
    E_FAIL, E_INVALIDARG, E_OUTOFMEMORY, ERROR_INSUFFICIENT_BUFFER, ERROR_MORE_DATA,
    ERROR_NOT_FOUND, ERROR_NOT_SUPPORTED, GetLastError,
};
use windows::Win32::System::Diagnostics::Debug::{ReadProcessMemory, WriteProcessMemory};
use windows::Win32::System::LibraryLoader::{GetModuleHandleW, GetProcAddress};
use windows::Win32::System::Threading::GetCurrentProcess;
use windows::core::{HRESULT, Result, s, w};

const INLINE_RUNS: usize = 16;

#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(super) struct BackingRun {
    pub(super) address: u64,
    pub(super) bytes: u64,
}

type Acquire = unsafe extern "system" fn(u64, u64, *mut u64, *mut u64) -> i32;
type AcquireRuns =
    unsafe extern "system" fn(u64, u64, *mut BackingRun, *mut u32, *mut u64) -> i32;
type Release = unsafe extern "system" fn(u64);

#[derive(Clone, Copy)]
pub(super) struct Exports {
    acquire: Acquire,
    acquire_runs: Option<AcquireRuns>,
    release: Release,
}

impl Exports {
    #[cfg(test)]
    pub(super) fn new(acquire: Acquire, release: Release) -> Self {
        Self { acquire, acquire_runs: None, release }
    }

    #[cfg(test)]
    pub(super) fn with_runs(acquire: Acquire, acquire_runs: AcquireRuns, release: Release) -> Self {
        Self { acquire, acquire_runs: Some(acquire_runs), release }
    }
}

fn lookup() -> Option<Exports> {
    // SAFETY: names match the XMem physical-backing exports documented above.
    unsafe {
        let module = GetModuleHandleW(w!("xmem.dll")).ok()?;
        let acquire = GetProcAddress(module, s!("TuxBoxAcquirePhysicalBacking"))?;
        let acquire_runs = GetProcAddress(module, s!("TuxBoxAcquirePhysicalBackingRuns")).map(
            |symbol| core::mem::transmute::<unsafe extern "system" fn() -> isize, AcquireRuns>(symbol),
        );
        let release = GetProcAddress(module, s!("TuxBoxReleasePhysicalBacking"))?;
        Some(Exports {
            acquire: core::mem::transmute::<unsafe extern "system" fn() -> isize, Acquire>(acquire),
            acquire_runs,
            release: core::mem::transmute::<unsafe extern "system" fn() -> isize, Release>(release),
        })
    }
}

#[cfg(test)]
pub(super) static TEST_EXPORTS: std::sync::Mutex<Option<Exports>> = std::sync::Mutex::new(None);

fn exports() -> Option<Exports> {
    #[cfg(test)]
    if let Some(exports) = *TEST_EXPORTS.lock().unwrap_or_else(|e| e.into_inner()) {
        return Some(exports);
    }
    // The title links XMem, so it is loaded before the first device exists.
    static EXPORTS: OnceLock<Option<Exports>> = OnceLock::new();
    *EXPORTS.get_or_init(lookup)
}

/// Keeps an XMem backing mapped. Drop it only after the native heap.
pub(in super::super) struct Hold {
    release: Release,
    cookie: u64,
}

impl Drop for Hold {
    fn drop(&mut self) {
        // SAFETY: the cookie came from the matching acquire and is released once.
        unsafe { (self.release)(self.cookie) };
    }
}

/// Where to import a guest range from.
pub(super) struct Backing {
    pub(super) address: u64,
    pub(super) hold: Option<Hold>,
}

/// Contiguous or piece-wise private host backing behind a guest range.
pub(super) enum AcquiredBacking {
    Contiguous(Backing),
    Scattered {
        runs: Vec<BackingRun>,
        hold: Hold,
    },
}

fn log_redirect(base: u64, address: u64) {
    if address != base {
        static REDIRECTED: std::sync::Once = std::sync::Once::new();
        crate::diagnostics::log_once(&REDIRECTED, || {
            "game memory is imported through its XMem backing".into()
        });
    }
}

fn acquire_runs_list(
    acquire_runs: AcquireRuns,
    release: Release,
    base: u64,
    size: u64,
) -> Result<Option<AcquiredBacking>> {
    let mut inline = [BackingRun { address: 0, bytes: 0 }; INLINE_RUNS];
    let mut overflow = Vec::new();
    let mut count = inline.len() as u32;
    let mut cookie = 0u64;
    // SAFETY: `inline` has `count` writable `BackingRun` slots.
    let slice = if unsafe { (acquire_runs)(base, size, inline.as_mut_ptr(), &mut count, &mut cookie) } != 0 {
        &inline[..]
    } else {
        let error = unsafe { GetLastError() };
        if error == ERROR_NOT_FOUND {
            return Ok(None);
        }
        if !matches!(error, ERROR_MORE_DATA | ERROR_INSUFFICIENT_BUFFER) || count <= inline.len() as u32 {
            return Err(HRESULT::from_win32(error.0).into());
        }
        if u64::from(count) > size / 65536 {
            return Err(E_INVALIDARG.into());
        }
        overflow.try_reserve_exact(count as usize).map_err(|_| E_OUTOFMEMORY)?;
        overflow.resize(count as usize, BackingRun { address: 0, bytes: 0 });
        // SAFETY: `overflow` has `count` writable `BackingRun` slots.
        if unsafe { (acquire_runs)(base, size, overflow.as_mut_ptr(), &mut count, &mut cookie) } == 0 {
            let retry_error = unsafe { GetLastError() };
            return if retry_error == ERROR_NOT_FOUND {
                Ok(None)
            } else {
                Err(HRESULT::from_win32(retry_error.0).into())
            };
        }
        &overflow[..]
    };
    let hold = Hold { release, cookie };
    let used = count as usize;
    if used == 0 || used > slice.len() {
        return Err(E_INVALIDARG.into());
    }
    let slice = &slice[..used];
    let mut total = 0u64;
    for run in slice {
        if run.address == 0
            || !run.address.is_multiple_of(65536)
            || run.bytes == 0
            || !run.bytes.is_multiple_of(65536)
            || run.address.checked_add(run.bytes).is_none()
        {
            return Err(E_INVALIDARG.into());
        }
        total = total.checked_add(run.bytes).ok_or(E_INVALIDARG)?;
    }
    if total != size {
        return Err(E_INVALIDARG.into());
    }
    if slice.len() == 1 {
        let address = slice[0].address;
        log_redirect(base, address);
        return Ok(Some(AcquiredBacking::Contiguous(Backing { address, hold: Some(hold) })));
    }
    let mut runs = Vec::new();
    runs.try_reserve_exact(slice.len()).map_err(|_| E_OUTOFMEMORY)?;
    runs.extend_from_slice(slice);
    static SCATTERED: std::sync::Once = std::sync::Once::new();
    crate::diagnostics::log_once(&SCATTERED, || {
        "game memory is imported through non-contiguous XMem backings".into()
    });
    Ok(Some(AcquiredBacking::Scattered { runs, hold }))
}

/// Resolve `base..base + size` to contiguous or piece-wise host memory for the GPU.
pub(super) fn acquire_backing(base: u64, size: u64) -> Result<AcquiredBacking> {
    let identity = || AcquiredBacking::Contiguous(Backing { address: base, hold: None });
    let Some(exports) = exports() else { return Ok(identity()) };
    let (mut address, mut cookie) = (0u64, 0u64);
    // SAFETY: both outputs are writable locals.
    if unsafe { (exports.acquire)(base, size, &mut address, &mut cookie) } != 0 {
        let hold = Hold { release: exports.release, cookie };
        if address == 0 || !address.is_multiple_of(65536) {
            return Err(E_INVALIDARG.into());
        }
        log_redirect(base, address);
        return Ok(AcquiredBacking::Contiguous(Backing { address, hold: Some(hold) }));
    }
    let error = unsafe { GetLastError() };
    if error == ERROR_NOT_FOUND {
        return Ok(identity());
    }
    if error == ERROR_NOT_SUPPORTED
        && let Some(acquire_runs) = exports.acquire_runs
    {
        return acquire_runs_list(acquire_runs, exports.release, base, size).map(|found| found.unwrap_or_else(identity));
    }
    Err(HRESULT::from_win32(error.0).into())
}

/// Resolve `base..base + size` to a single contiguous host backing range.
#[cfg(test)]
pub(super) fn acquire(base: u64, size: u64) -> Result<Backing> {
    match acquire_backing(base, size)? {
        AcquiredBacking::Contiguous(backing) => Ok(backing),
        AcquiredBacking::Scattered { .. } => Err(windows::Win32::Foundation::E_NOTIMPL.into()),
    }
}

fn acquired_runs(base: u64, size: u64) -> Result<Option<(Vec<BackingRun>, Hold)>> {
    match acquire_backing(base, size)? {
        AcquiredBacking::Contiguous(Backing { hold: None, .. }) => Ok(None),
        AcquiredBacking::Contiguous(Backing { address, hold: Some(hold) }) => {
            Ok(Some((vec![BackingRun { address, bytes: size }], hold)))
        }
        AcquiredBacking::Scattered { runs, hold } => Ok(Some((runs, hold))),
    }
}

fn visit_xmem_runs(
    address: u64,
    bytes: usize,
    mut f: impl FnMut(u64, usize, usize) -> Result<()>,
) -> Option<Result<()>> {
    if bytes == 0 {
        return Some(Ok(()));
    }
    let Some(end) = address.checked_add(bytes as u64) else {
        return Some(Err(E_INVALIDARG.into()));
    };
    let page_start = address & !0xffff;
    let Some(page_end) = end.checked_next_multiple_of(65536) else {
        return Some(Err(E_INVALIDARG.into()));
    };
    let (runs, _hold) = match acquired_runs(page_start, page_end - page_start) {
        Ok(Some(ok)) => ok,
        Ok(None) => return None,
        Err(error) => return Some(Err(error)),
    };
    let mut guest_cursor = page_start;
    for run in runs {
        let Some(guest_next) = guest_cursor.checked_add(run.bytes) else {
            return Some(Err(E_INVALIDARG.into()));
        };
        let copy_lo = guest_cursor.max(address);
        let copy_hi = guest_next.min(end);
        if copy_lo < copy_hi {
            let len = (copy_hi - copy_lo) as usize;
            let buf_off = (copy_lo - address) as usize;
            let host_addr = run.address + (copy_lo - guest_cursor);
            if let Err(err) = f(host_addr, buf_off, len) {
                return Some(Err(err));
            }
        }
        guest_cursor = guest_next;
    }
    Some(Ok(()))
}

pub(in super::super) fn is_backed_range(start: u64, end: u64) -> bool {
    if end <= start {
        return false;
    }
    let page_start = start & !0xffff;
    let Some(page_end) = end.checked_next_multiple_of(65536) else {
        return false;
    };
    matches!(acquired_runs(page_start, page_end - page_start), Ok(Some(_)))
}

pub(in super::super) fn try_read_xmem_bytes(address: u64, dst: &mut [u8]) -> Option<bool> {
    visit_xmem_runs(address, dst.len(), |host_addr, buf_off, len| {
        let mut got = 0usize;
        unsafe {
            ReadProcessMemory(
                GetCurrentProcess(),
                host_addr as *const _,
                dst[buf_off..buf_off + len].as_mut_ptr().cast(),
                len,
                Some(&mut got),
            )
        }?;
        if got != len {
            return Err(E_FAIL.into());
        }
        Ok(())
    })
    .map(|res| res.is_ok())
}

pub(in super::super) fn read_guest_bytes(address: u64, dst: &mut [u8]) -> bool {
    if let Some(ok) = try_read_xmem_bytes(address, dst) {
        return ok;
    }
    if dst.is_empty() {
        return true;
    }
    let mut got = 0usize;
    let res = unsafe {
        ReadProcessMemory(
            GetCurrentProcess(),
            address as *const _,
            dst.as_mut_ptr().cast(),
            dst.len(),
            Some(&mut got),
        )
    };
    res.is_ok() && got == dst.len()
}

pub(in super::super) fn try_write_xmem_bytes(address: u64, src: &[u8]) -> Option<Result<()>> {
    visit_xmem_runs(address, src.len(), |host_addr, buf_off, len| {
        let mut written = 0usize;
        unsafe {
            WriteProcessMemory(
                GetCurrentProcess(),
                host_addr as *const _,
                src[buf_off..buf_off + len].as_ptr().cast(),
                len,
                Some(&mut written),
            )
        }?;
        if written != len {
            return Err(E_FAIL.into());
        }
        Ok(())
    })
}

#[cfg(test)]
#[path = "../../tests/device/heap_backing.rs"]
mod tests;
