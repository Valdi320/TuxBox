use super::*;
use core::ffi::c_void;
use crate::physical_pages::copy_mappings::{Batch, PageRange, copy_page_mappings};
use crate::physical_pages::{allocate, free, map};
use crate::virtual_memory::virtual_alloc;
use std::sync::Mutex;
use windows_sys::Win32::Foundation::GetLastError;
use windows_sys::Win32::System::Memory::{
    MEM_COMMIT as M_COMMIT, MEM_PRIVATE, MEM_RESERVE as M_RESERVE, MEMORY_BASIC_INFORMATION,
    PAGE_NOACCESS, VirtualQuery,
};

static SERIAL: Mutex<()> = Mutex::new(());

fn reserve_aperture(pages: usize) -> *mut c_void {
    unsafe { virtual_alloc(ptr::null_mut(), pages * PAGE, 0x2040_2000, 0x23, 0x2404) }
}

fn query(addr: usize) -> MEMORY_BASIC_INFORMATION {
    let mut info = MEMORY_BASIC_INFORMATION::default();
    assert_ne!(unsafe { VirtualQuery(addr as _, &mut info, size_of_val(&info)) }, 0);
    info
}

#[test]
fn cpu_view_is_private_and_acquired_directly_while_foreign_is_not_found() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let plain = unsafe { VirtualAlloc(ptr::null(), PAGE, M_RESERVE | M_COMMIT, PAGE_READWRITE) };
    assert!(!plain.is_null());
    let (mut addr, mut hold) = (0u64, 0u64);
    assert_eq!(
        unsafe { acquire_physical_backing(plain.addr() as u64, PAGE as u64, &mut addr, &mut hold) },
        0
    );
    assert_eq!(unsafe { GetLastError() }, ERROR_NOT_FOUND);
    assert_ne!(unsafe { VirtualFree(plain, 0, MEM_RELEASE) }, 0);

    let mut tokens = [0u64; 2];
    let mut count = 2usize;
    assert_eq!(unsafe { allocate(super::super::FLAGS, &mut count, tokens.as_mut_ptr()) }, 1);
    let cpu = reserve_aperture(2);
    assert!(!cpu.is_null());
    assert_eq!(unsafe { map(cpu, 2, tokens.as_ptr()) }, cpu);

    let info = query(cpu.addr());
    assert_eq!(info.State, M_COMMIT);
    assert_eq!(info.Type, MEM_PRIVATE);
    assert_eq!(info.Protect, PAGE_READWRITE);

    assert_eq!(
        unsafe { acquire_physical_backing(cpu.addr() as u64, (2 * PAGE) as u64, &mut addr, &mut hold) },
        1
    );
    assert_eq!(addr, cpu.addr() as u64);
    assert_ne!(hold, 0);

    // Unmapping while held preserves the backing memory in place until hold release.
    assert_eq!(unsafe { map(cpu, 2, ptr::null()) }, cpu);
    assert_eq!(query(cpu.addr()).State, M_COMMIT);
    release_physical_backing(hold);
    assert_eq!(query(cpu.addr()).State, M_RESERVE);

    assert_eq!(unsafe { free(2, tokens.as_ptr()) }, 1);
    assert_ne!(unsafe { VirtualFree(cpu, 0, MEM_RELEASE) }, 0);
}

#[test]
fn gpu_alias_resolves_to_held_cpu_view_even_after_cpu_unmap_and_free() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let mut tokens = [0u64; 2];
    let mut count = 2usize;
    assert_eq!(unsafe { allocate(super::super::FLAGS, &mut count, tokens.as_mut_ptr()) }, 1);
    let cpu = reserve_aperture(2);
    let gpu = reserve_aperture(2);
    assert_eq!(unsafe { map(cpu, 2, tokens.as_ptr()) }, cpu);
    unsafe {
        cpu.cast::<u32>().write(0x1122_3344);
        cpu.cast::<u8>().add(PAGE).cast::<u32>().write(0x5566_7788);
    }

    let range = PageRange { mode: 0, source: 0, count: 2 };
    let batch = Batch { destination: gpu.addr(), count: 1, ranges: &range };
    assert_eq!(unsafe { copy_page_mappings(1, &batch, cpu.addr(), 2) }, 0);
    // GPU alias must also use MEM_PRIVATE pages and not alter CPU-written contents.
    assert_eq!(query(gpu.addr()).Type, MEM_PRIVATE);

    let (mut addr, mut hold) = (0u64, 0u64);
    assert_eq!(
        unsafe { acquire_physical_backing(gpu.addr() as u64, (2 * PAGE) as u64, &mut addr, &mut hold) },
        1
    );
    assert_eq!(addr, cpu.addr() as u64);

    // Unmap CPU view & free physical tokens while GPU hold lives: backing must stay readable!
    assert_eq!(unsafe { map(cpu, 2, ptr::null()) }, cpu);
    assert_eq!(unsafe { free(2, tokens.as_ptr()) }, 1);
    assert_eq!(query(addr as usize).State, M_COMMIT);
    assert_eq!(unsafe { (addr as *const u32).read() }, 0x1122_3344);
    assert_eq!(unsafe { ((addr as usize + PAGE) as *const u32).read() }, 0x5566_7788);

    release_physical_backing(hold);
    assert_eq!(query(cpu.addr()).State, M_RESERVE);
    assert_eq!(query(gpu.addr()).State, M_RESERVE);
    assert_ne!(unsafe { VirtualFree(cpu, 0, MEM_RELEASE) }, 0);
    assert_ne!(unsafe { VirtualFree(gpu, 0, MEM_RELEASE) }, 0);
}

#[test]
fn gpu_alias_only_and_conflicting_runs_allocate_private_arena_and_split_runs() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let mut tokens = [0u64; 3];
    let mut count = 3usize;
    assert_eq!(unsafe { allocate(super::super::FLAGS, &mut count, tokens.as_mut_ptr()) }, 1);
    let cpu = reserve_aperture(3);
    let gpu_a = reserve_aperture(3);
    let gpu_b = reserve_aperture(3);
    assert_eq!(unsafe { map(cpu, 3, tokens.as_ptr()) }, cpu);
    for i in 0..3usize {
        unsafe { cpu.cast::<u8>().add(i * PAGE).cast::<u32>().write(0xabcd_0000 + i as u32) };
    }
    let r0 = PageRange { mode: 0, source: 0, count: 3 };
    let b0 = Batch { destination: gpu_a.addr(), count: 1, ranges: &r0 };
    assert_eq!(unsafe { copy_page_mappings(1, &b0, cpu.addr(), 3) }, 0);

    // Second alias maps in conflicting order [2, 0, 1].
    let r1 = [
        PageRange { mode: 0, source: 2, count: 1 },
        PageRange { mode: 0, source: 0, count: 2 },
    ];
    let b1 = Batch { destination: gpu_b.addr(), count: 2, ranges: r1.as_ptr() };
    assert_eq!(unsafe { copy_page_mappings(1, &b1, cpu.addr(), 3) }, 0);

    // Unmap CPU view BEFORE any acquire so pages become GPU-alias-only.
    assert_eq!(unsafe { map(cpu, 3, ptr::null()) }, cpu);

    // Acquiring gpu_a allocates a single contiguous 3-page PrivateArena.
    let (mut addr_a, mut hold_a) = (0u64, 0u64);
    assert_eq!(
        unsafe { acquire_physical_backing(gpu_a.addr() as u64, (3 * PAGE) as u64, &mut addr_a, &mut hold_a) },
        1
    );
    assert_eq!(query(addr_a as usize).Type, MEM_PRIVATE);
    for i in 0..3usize {
        assert_eq!(unsafe { ((addr_a as usize + i * PAGE) as *const u32).read() }, 0xabcd_0000 + i as u32);
    }

    // Acquiring conflicting order gpu_b fails contiguous query with ERROR_NOT_SUPPORTED,
    // and returns 2 coalesced runs ([page 2], [page 0, page 1]) via Runs + ERROR_MORE_DATA check.
    let (mut dummy, mut hold_b) = (0u64, 0u64);
    assert_eq!(
        unsafe { acquire_physical_backing(gpu_b.addr() as u64, (3 * PAGE) as u64, &mut dummy, &mut hold_b) },
        0
    );
    assert_eq!(unsafe { GetLastError() }, ERROR_NOT_SUPPORTED);

    let mut runs = [BackingRun { address: 0, bytes: 0 }; 4];
    let mut run_count = 1u32;
    assert_eq!(
        unsafe {
            acquire_physical_backing_runs(
                gpu_b.addr() as u64,
                (3 * PAGE) as u64,
                runs.as_mut_ptr(),
                &mut run_count,
                &mut hold_b,
            )
        },
        0
    );
    assert_eq!(unsafe { GetLastError() }, ERROR_MORE_DATA);
    assert_eq!(run_count, 2);

    run_count = 4;
    assert_eq!(
        unsafe {
            acquire_physical_backing_runs(
                gpu_b.addr() as u64,
                (3 * PAGE) as u64,
                runs.as_mut_ptr(),
                &mut run_count,
                &mut hold_b,
            )
        },
        1
    );
    assert_eq!(run_count, 2);
    assert_eq!(runs[0], BackingRun { address: addr_a + 2 * PAGE as u64, bytes: PAGE as u64 });
    assert_eq!(runs[1], BackingRun { address: addr_a, bytes: (2 * PAGE) as u64 });

    // Freeing all physical tokens & apertures leaves the PrivateArena readable until BOTH holds drop.
    assert_eq!(unsafe { free(3, tokens.as_ptr()) }, 1);
    assert_eq!(query(addr_a as usize).State, M_COMMIT);
    release_physical_backing(hold_a);
    assert_eq!(query(addr_a as usize).State, M_COMMIT);
    release_physical_backing(hold_b);

    assert_ne!(unsafe { VirtualFree(cpu, 0, MEM_RELEASE) }, 0);
    assert_ne!(unsafe { VirtualFree(gpu_a, 0, MEM_RELEASE) }, 0);
    assert_ne!(unsafe { VirtualFree(gpu_b, 0, MEM_RELEASE) }, 0);
}

#[test]
fn mapping_second_cpu_view_promotes_token_from_private_to_shared_section() {
    let _guard = SERIAL.lock().unwrap_or_else(|e| e.into_inner());
    let mut token = 0u64;
    let mut count = 1usize;
    assert_eq!(unsafe { allocate(super::super::FLAGS, &mut count, &mut token) }, 1);
    let a = reserve_aperture(1);
    let b = reserve_aperture(1);
    assert_eq!(unsafe { map(a, 1, &token) }, a);
    assert_eq!(query(a.addr()).Type, MEM_PRIVATE);
    unsafe { a.cast::<u32>().write(0xdead_beef) };

    assert_eq!(unsafe { map(b, 1, &token) }, b);
    assert_eq!(unsafe { b.cast::<u32>().read() }, 0xdead_beef);
    unsafe { b.cast::<u32>().write(0xcafe_babe) };
    assert_eq!(unsafe { a.cast::<u32>().read() }, 0xcafe_babe);

    assert_eq!(unsafe { map(a, 1, ptr::null()) }, a);
    assert_eq!(unsafe { map(b, 1, ptr::null()) }, b);
    assert_eq!(query(a.addr()).State, M_RESERVE);
    assert_eq!(query(a.addr()).AllocationProtect, PAGE_NOACCESS);
    assert_eq!(unsafe { free(1, &token) }, 1);
    assert_ne!(unsafe { VirtualFree(a, 0, MEM_RELEASE) }, 0);
    assert_ne!(unsafe { VirtualFree(b, 0, MEM_RELEASE) }, 0);
}
