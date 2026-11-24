//! Wrapped root signatures and the Xbox extended compute pipeline factory.
use super::{Device, Out, identity, wrap_pageable};
use crate::proxy::{Proxy, Slots, allocate, check_output, child_slots, unwrap};
use core::{ffi::c_void, mem::ManuallyDrop, slice};
use windows::Win32::Foundation::{E_INVALIDARG, E_NOTIMPL, E_POINTER};
use windows::Win32::Graphics::Direct3D12::*;
use windows::core::{GUID, HRESULT, Interface};

pub(crate) use super::compute_table::ComputeTable;
pub(crate) use super::compute_scene::{RootSource, SceneRoot, SceneSource};

static ROOT: Slots<8> = child_slots::<ID3D12RootSignature, RootSource, 8>();
pub(crate) type Root = Proxy<ID3D12RootSignature, RootSource>;
pub(crate) type Pipeline = Proxy<ID3D12PipelineState, PipelineData>;
// Inline console SetPipelineState copies 32 bytes starting at +0x10 from
// this object to the guest command list. Desktop command lists use slot 25
// with an empty packet buffer, so no Xbox command packets are advertised.
#[repr(C)]
#[derive(Default)]
pub(crate) struct PipelineData {
    reserved: usize,
    packets: [u8; 32],
    pub(crate) stencil_slot: Option<u32>,
    pub(crate) draw_trace: Option<std::sync::Arc<crate::diagnostics::RasterDraw>>,
    pub(crate) scene_root: Option<SceneRoot>,
    pub(crate) reflected: Option<std::sync::Arc<ComputeTable>>,
    pub(crate) raster_table: Option<std::sync::Arc<ComputeTable>>,
}

const _: () = assert!(
    core::mem::offset_of!(Proxy<ID3D12PipelineState, PipelineData>, state)
        + core::mem::offset_of!(PipelineData, packets)
        == 0x10
);
// GetInformationX is slot 11. Two methods follow it. A shorter table would
// read off the end, so those calls fail closed.
pub(super) static PIPELINE: Slots<14> = {
    let mut slots = child_slots::<ID3D12PipelineState, PipelineData, 14>();
    slots[11] = crate::slot!(scratch_info);
    slots
};

// FidelityFX queries a blob for the console pipeline's scratch size. Desktop
// pipelines manage their own scratch. The caller skips the blob on failure.
unsafe extern "system" fn scratch_info(
    _this: &Proxy<ID3D12PipelineState, PipelineData>,
    _arg1: usize,
    _arg2: usize,
    blob: Out,
) -> HRESULT {
    if blob.is_null() {
        return E_POINTER;
    }
    // SAFETY: the caller provided storage for one COM interface pointer.
    unsafe { blob.write(core::ptr::null_mut()) };
    E_NOTIMPL
}

// A null root signature is valid for a shader with an embedded signature.
// The caller keeps every non-null guest object alive for the returned borrow.
pub(crate) unsafe fn root_proxy<'a>(
    guest: *mut c_void,
    device: *mut c_void,
) -> Result<Option<&'a Root>, HRESULT> {
    if guest.is_null() {
        return Ok(None);
    }
    // SAFETY: unwrap checks both the guest vtable and device identity.
    unsafe { unwrap::<ID3D12RootSignature, RootSource>(guest, device, &ROOT) }
        .map(Some)
        .ok_or(E_INVALIDARG)
}

pub(crate) unsafe fn root_signature<'a>(
    guest: *mut c_void,
    device: *mut c_void,
) -> Result<Option<&'a ID3D12RootSignature>, HRESULT> {
    unsafe { root_proxy(guest, device) }.map(|root| root.map(|p| &p.native))
}

pub(crate) unsafe fn pipeline<'a>(
    guest: *mut c_void,
    device: *mut c_void,
) -> Result<Option<&'a Proxy<ID3D12PipelineState, PipelineData>>, HRESULT> {
    if guest.is_null() {
        return Ok(None);
    }
    // SAFETY: as with root_signature, the caller owns the live guest reference.
    unsafe { unwrap::<ID3D12PipelineState, PipelineData>(guest, device, &PIPELINE) }
        .map(Some)
        .ok_or(E_INVALIDARG)
}

pub(super) unsafe extern "system" fn create_root_signature(
    this: &Device,
    node: u32,
    blob: *const u8,
    length: usize,
    iid: *const GUID,
    out: Out,
) -> HRESULT {
    // SAFETY: the guest supplies a readable IID and writable output slot.
    let hr = unsafe { check_output::<ID3D12RootSignature>(iid, out) };
    if hr.is_err() {
        return hr;
    }
    if blob.is_null() || length == 0 || length > isize::MAX as usize {
        return E_INVALIDARG;
    }
    // SAFETY: serialized signature bytes are borrowed only for this call.
    let bytes = unsafe { slice::from_raw_parts(blob, length) };
    match unsafe {
        this.native
            .CreateRootSignature::<ID3D12RootSignature>(node, bytes)
    } {
        // SAFETY: the returned guest child owns both its native interface and device.
        Ok(native) => unsafe {
            allocate(
                &ROOT,
                &[ID3D12DeviceChild::IID, ID3D12RootSignature::IID],
                native,
                Some(&identity(this)),
                RootSource::new(bytes),
                out,
            )
        },
        Err(error) => error.code(),
    }
}

// Slot 67 receives a serialized console PSO. FidelityFX retries slot 62 with a
// shader descriptor if its precompiled ACO pipeline cannot be deserialized.
pub(super) unsafe extern "system" fn deserialize_pipeline_x(
    _this: &Device,
    _desc: *const c_void,
    iid: *const GUID,
    out: Out,
) -> HRESULT {
    // SAFETY: the guest supplies a readable IID and writable output slot.
    let hr = unsafe { check_output::<ID3D12PipelineState>(iid, out) };
    if hr.is_err() { hr } else { E_NOTIMPL }
}

// Slot 62 takes a compute descriptor followed by an array of Xbox extensions.
// Portable DXIL and a standard serialized root signature work with zero extensions.
pub(super) unsafe extern "system" fn create_pipeline_x(
    this: &Device,
    desc: *const D3D12_COMPUTE_PIPELINE_STATE_DESC,
    extension_count: u32,
    _extensions: *const c_void,
    iid: *const GUID,
    out: Out,
) -> HRESULT {
    // SAFETY: the guest supplies a readable IID and writable output slot.
    let hr = unsafe { check_output::<ID3D12PipelineState>(iid, out) };
    if hr.is_err() {
        return hr;
    }
    let Some(desc) = (unsafe { desc.as_ref() }) else {
        return E_INVALIDARG;
    };
    if extension_count != 0 || desc.CachedPSO.CachedBlobSizeInBytes != 0 {
        return E_NOTIMPL;
    }
    if desc.CS.pShaderBytecode.is_null()
        || desc.CS.BytecodeLength == 0
        || desc.CS.BytecodeLength > isize::MAX as usize
    {
        return E_INVALIDARG;
    }
    let guest = desc
        .pRootSignature
        .as_ref()
        .map_or(core::ptr::null_mut(), Interface::as_raw);
    // SAFETY: the descriptor borrows its root signature for pipeline creation.
    let root = match unsafe { root_proxy(guest, identity(this).as_raw()) } {
        Ok(root) => root,
        Err(error) => return error,
    };
    // SAFETY: the guest retains its shader bytes until this synchronous call returns.
    let bytes =
        unsafe { slice::from_raw_parts(desc.CS.pShaderBytecode.cast(), desc.CS.BytecodeLength) };
    use super::compute_table::{Program, TableLayout};
    let layout = match root.map(|r| super::compute_table::reflected(
        &this.native, &r.state, Program::Compute(bytes), desc.NodeMask,
    )).transpose() {
        Ok(layout) => layout,
        Err(error) => return error.code(),
    };
    let (translation, reflected) = match layout {
        Some(TableLayout::Reflected(table)) => {
            // Embedded RTS0 cannot be replaced. The translator reads that original
            // root and fills only the slots the appended signature adds.
            let code = super::shader_cache::compute_with_root(bytes, table.layout.payload())
                .or_else(|| super::shader_cache::prepared(bytes));
            let Some(code) = code else {
                super::compute_table::capture_stage_root(bytes, &table.original);
                crate::diagnostics::compute_failure(bytes, E_NOTIMPL.0);
                return unsafe { null_compute(this, root.map(|p| &p.native), desc, out) }.unwrap_or(E_NOTIMPL);
            };
            (Some(code), Some(table))
        }
        Some(TableLayout::Original) => (root.and_then(|r| {
            super::shader_cache::compute_for_original_root(bytes, r.state.payload())
        }), None),
        _ => (super::shader_cache::prepared(bytes).or_else(|| {
            super::shader_cache::compute_with_root(bytes, root?.state.payload())
        }), None),
    };
    let scene_root = if let Some(translated) = &translation {
        match super::compute_scene::private_root(
            &this.native,
            bytes,
            translated,
            root.map(|p| &p.state),
            desc.NodeMask,
        ) {
            Ok(root) => root,
            Err(e) => return e.code(),
        }
    } else {
        None
    };
    let code = translation.as_deref().unwrap_or(bytes);
    let label = super::shader_cache::source_digest(bytes)
        .map(|hash| format!("CS {}", &hash[..8]))
        .unwrap_or_else(|| "CS".to_string());
    let mut host = D3D12_COMPUTE_PIPELINE_STATE_DESC {
        pRootSignature: ManuallyDrop::new(
            scene_root
                .as_ref()
                .map(|s| &s.native)
                .or_else(|| reflected.as_ref().map(|t| &t.native))
                .or(root.map(|p| &p.native))
                .cloned(),
        ),
        CS: D3D12_SHADER_BYTECODE {
            pShaderBytecode: code.as_ptr().cast(),
            BytecodeLength: code.len(),
        },
        NodeMask: desc.NodeMask,
        Flags: desc.Flags,
        ..Default::default()
    };
    // The driver compile must own its device, root signature, and shader bytes:
    // a deadline can return while that compile is still writing the GPU cache.
    let signature = core::mem::ManuallyDrop::into_inner(host.pRootSignature.clone());
    let compile = |shader: &[u8]| {
        let device = this.native.clone();
        let signature = signature.clone();
        let shader = shader.to_vec();
        let node_mask = desc.NodeMask;
        let flags = desc.Flags;
        crate::big_stack::run_deadline(&label, move || {
            owned_compile(device, signature, shader, node_mask, flags)
        })
    };
    // Boot 174727 substituted null pipelines for translated shaders whose
    // driver compile passed 20s. Those null pipelines have no paged layout, so
    // the dispatch that precedes the account-picker draws was dropped.
    let mut native = if translation.is_some() {
        static WAIT: std::sync::Once = std::sync::Once::new();
        crate::diagnostics::log_once(&WAIT, || {
            "translated compute pipelines wait for the driver compile".to_string()
        });
        let device = this.native.clone();
        let signature = signature.clone();
        let shader = code.to_vec();
        let node_mask = desc.NodeMask;
        let flags = desc.Flags;
        crate::big_stack::run_logged(&label, move || {
            owned_compile(device, signature, shader, node_mask, flags)
        })
    } else {
        match compile(code) {
            crate::big_stack::PipelineWait::Ready(result) => result,
            crate::big_stack::PipelineWait::Deferred => {
                // The original guest shader would be a second long driver compile.
                unsafe { ManuallyDrop::drop(&mut host.pRootSignature) };
                return unsafe { null_compute(this, root.map(|p| &p.native), desc, out) }.unwrap_or(E_NOTIMPL);
            }
        }
    };
    if translation.is_some() && reflected.is_none() && scene_root.is_none() && native.is_err() {
        // Structural checks cannot validate DXIL instructions or root bindings.
        // Let the adapter handle the original guest shader if it rejects the cache.
        native = match compile(bytes) {
            crate::big_stack::PipelineWait::Ready(result) => result,
            crate::big_stack::PipelineWait::Deferred => {
                unsafe { ManuallyDrop::drop(&mut host.pRootSignature) };
                return unsafe { null_compute(this, root.map(|p| &p.native), desc, out) }.unwrap_or(E_NOTIMPL);
            }
        };
    }
    unsafe { ManuallyDrop::drop(&mut host.pRootSignature) };
    if let Err(error) = &native {
        if let Some(root) = root { super::compute_table::capture_stage_root(bytes, &root.state); }
        crate::diagnostics::compute_failure(bytes, error.code().0);
        if let Some(status) = unsafe { null_compute(this, root.map(|p| &p.native), desc, out) } {
            return status;
        }
    } else if let Some(table) = &reflected {
        static FIRST: std::sync::Once = std::sync::Once::new();
        crate::diagnostics::log_once(&FIRST, || format!(
            "created compute pipeline with {} reflected FSSM descriptor slots", table.lengths.iter().map(|&v| u64::from(v)).sum::<u64>()
        ));
    }
    unsafe {
        wrap_pageable(
            native,
            &PIPELINE,
            this,
            PipelineData {
                scene_root,
                reflected,
                ..Default::default()
            },
            out,
        )
    }
}

/// Compile one compute pipeline from copies the helper thread owns.
///
/// The guest descriptor can be released as soon as the caller returns. A
/// deadline does that while the driver is still compiling, so the bytes and
/// the root signature in that descriptor must not be the ones the driver reads.
fn owned_compile(
    device: ID3D12Device,
    root: Option<ID3D12RootSignature>,
    shader: Vec<u8>,
    node_mask: u32,
    flags: D3D12_PIPELINE_STATE_FLAGS,
) -> windows::core::Result<ID3D12PipelineState> {
    let mut descriptor = D3D12_COMPUTE_PIPELINE_STATE_DESC {
        pRootSignature: ManuallyDrop::new(root),
        CS: D3D12_SHADER_BYTECODE {
            pShaderBytecode: shader.as_ptr().cast(),
            BytecodeLength: shader.len(),
        },
        NodeMask: node_mask,
        Flags: flags,
        ..Default::default()
    };
    // SAFETY: `shader` and `descriptor.pRootSignature` stay alive until this
    // call returns. `device` is an AddRef held by this thread.
    let result = unsafe { device.CreateComputePipelineState(&descriptor) };
    unsafe { ManuallyDrop::drop(&mut descriptor.pRootSignature) };
    result
}

/// Create a resource-free compute pipeline on the guest root (see `fallback`).
unsafe fn null_compute(
    this: &Device, root: Option<&ID3D12RootSignature>, desc: &D3D12_COMPUTE_PIPELINE_STATE_DESC, out: Out,
) -> Option<HRESULT> {
    use super::fallback::{Stage, bytecode, enabled, shader, substituted};
    if !enabled() { return None; }
    let mut host = D3D12_COMPUTE_PIPELINE_STATE_DESC {
        pRootSignature: ManuallyDrop::new(root.cloned()),
        CS: bytecode(shader(Stage::Compute)?),
        NodeMask: desc.NodeMask,
        Flags: desc.Flags,
        ..Default::default()
    };
    let native = crate::big_stack::run_logged("null CS", || unsafe { this.native.CreateComputePipelineState::<ID3D12PipelineState>(&host) });
    unsafe { ManuallyDrop::drop(&mut host.pRootSignature) };
    if native.is_err() { return None; }
    if let Ok(pipeline) = &native {
        let code = unsafe { std::slice::from_raw_parts(desc.CS.pShaderBytecode.cast::<u8>(), desc.CS.BytecodeLength) };
        if crate::diagnostics::display::enabled() && let Some(digest) = super::shader_cache::source_digest(code) {
            crate::diagnostics::display::mark_null_compute(pipeline.as_raw(), digest);
        }
    }
    substituted("compute");
    Some(unsafe { wrap_pageable(native, &PIPELINE, this, PipelineData::default(), out) })
}
