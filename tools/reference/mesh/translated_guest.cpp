#include "frame_stream.h"
#include <fstream>
#include <vector>
#include <cstring>
using frame_abi::check;
using frame_abi::ComPtr;

using Stream = frame_abi::MeshPipelineStream;

static ComPtr<ID3D12Resource> buffer(ID3D12Device* d, D3D12_HEAP_TYPE type) {
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = type;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_BUFFER;
    desc.Width = 8 * 8 * 4;
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = desc.SampleDesc.Count = 1;
    desc.Layout = D3D12_TEXTURE_LAYOUT_ROW_MAJOR;
    if (type == D3D12_HEAP_TYPE_DEFAULT)
        desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS;
    ComPtr<ID3D12Resource> result;
    check(d->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc,
                                     type == D3D12_HEAP_TYPE_UPLOAD ? D3D12_RESOURCE_STATE_GENERIC_READ
                                              : D3D12_RESOURCE_STATE_COPY_DEST,
                                     nullptr, IID_PPV_ARGS(&result)),
          "create buffer");
    return result;
}

static std::vector<char> load(const char* path) {
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    const auto size = stream.tellg();
    if (size <= 0 || size > 16 * 1024 * 1024)
        throw std::runtime_error("invalid shader file");
    std::vector<char> data(static_cast<size_t>(size));
    stream.seekg(0);
    if (!stream.read(data.data(), size))
        throw std::runtime_error("shader read");
    return data;
}

static void run(const char* name, bool draw, bool clipped, FILE* file) {
    const auto mesh = load((std::string(name) + ".cso").c_str());
    const auto pixel = load("pixel.cso");
    ComPtr<ID3D12Device> provider;
    check(D3D12CreateDevice(nullptr, D3D_FEATURE_LEVEL_12_0, IID_PPV_ARGS(&provider)), "desktop device");
    D3D12_ROOT_PARAMETER parameter{};
    parameter.ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    D3D12_ROOT_SIGNATURE_DESC desc{1, &parameter, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> blob;
    check(D3D12SerializeRootSignature(&desc, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr), "serialize root");
    ComPtr<ID3D12RootSignature> root;
    check(provider->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(&root)),
          "create root");
    ComPtr<ID3D12Device2> owner;
    check(root->GetDevice(IID_PPV_ARGS(&owner)), "get owning device");
    ComPtr<IDXGIFactory4> factory;
    ComPtr<IDXGIAdapter1> adapter;
    DXGI_ADAPTER_DESC1 gpu{};
    check(CreateDXGIFactory1(IID_PPV_ARGS(&factory)), "DXGI factory");
    check(factory->EnumAdapterByLuid(owner->GetAdapterLuid(), IID_PPV_ARGS(&adapter)), "DXGI adapter");
    check(adapter->GetDesc1(&gpu), "GPU description");
    std::fprintf(file, "%s_%s_adapter=%04x:%04x\n", name, clipped ? "clipped" : "full", gpu.VendorId, gpu.DeviceId);
    Stream stream;
    stream.root.payload = root.Get();
    stream.ms.payload = {mesh.data(), mesh.size()};
    stream.ps.payload = {pixel.data(), pixel.size()};
    stream.raster.payload.FillMode = D3D12_FILL_MODE_SOLID;
    stream.raster.payload.CullMode = D3D12_CULL_MODE_NONE;
    stream.raster.payload.DepthClipEnable = TRUE;
    stream.ms.tag = D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_MS;
    stream.as.tag = D3D12_PIPELINE_STATE_SUBOBJECT_TYPE_AS;
    D3D12_PIPELINE_STATE_STREAM_DESC pipelineDesc{offsetof(Stream, extension), &stream};
    ComPtr<ID3D12PipelineState> pso;
    check(owner->CreatePipelineState(&pipelineDesc, IID_PPV_ARGS(&pso)), "desktop mesh pipeline");
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList6> list;
    D3D12_COMMAND_QUEUE_DESC q{};
    check(owner->CreateCommandQueue(&q, IID_PPV_ARGS(&queue)), "queue");
    check(owner->CreateCommandAllocator(q.Type, IID_PPV_ARGS(&allocator)), "allocator");
    check(owner->CreateCommandList(0, q.Type, allocator.Get(), pso.Get(), IID_PPV_ARGS(&list)), "list6");
    auto output = buffer(owner.Get(), D3D12_HEAP_TYPE_DEFAULT);
    auto readback = buffer(owner.Get(), D3D12_HEAP_TYPE_READBACK);
    auto upload = buffer(owner.Get(), D3D12_HEAP_TYPE_UPLOAD);
    constexpr UINT seed = 0x13572468;
    void* mapped{};
    D3D12_RANGE none{};
    check(upload->Map(0, &none, &mapped), "map seed");
    for (UINT i = 0; i < 64; ++i) static_cast<UINT*>(mapped)[i] = seed;
    upload->Unmap(0, nullptr);
    list->CopyResource(output.Get(), upload.Get());
    D3D12_RESOURCE_BARRIER before{};
    before.Transition = {output.Get(), 0, D3D12_RESOURCE_STATE_COPY_DEST,
                         D3D12_RESOURCE_STATE_UNORDERED_ACCESS};
    list->ResourceBarrier(1, &before);
    list->SetGraphicsRootSignature(root.Get());
    list->SetGraphicsRootUnorderedAccessView(0, output->GetGPUVirtualAddress());
    D3D12_VIEWPORT viewport{0, 0, 8, 8, 0, 1};
    D3D12_RECT scissor = clipped ? D3D12_RECT{2, 1, 6, 5} : D3D12_RECT{0, 0, 8, 8};
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->DispatchMesh(1, 1, 1);
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Transition = {output.Get(), 0, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                          D3D12_RESOURCE_STATE_COPY_SOURCE};
    list->ResourceBarrier(1, &barrier);
    list->CopyResource(readback.Get(), output.Get());
    check(list->Close(), "close");
    ID3D12CommandList* lists[] = {list.Get()};
    queue->ExecuteCommandLists(1, lists);
    ComPtr<ID3D12Fence> fence;
    check(owner->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "fence");
    check(queue->Signal(fence.Get(), 1), "signal");
    HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
    if (!event)
        throw std::runtime_error("event");
    HRESULT hr = fence->SetEventOnCompletion(1, event);
    DWORD wait = SUCCEEDED(hr) ? WaitForSingleObject(event, 15000) : WAIT_FAILED;
    CloseHandle(event);
    check(hr, "signal event");
    if (wait != WAIT_OBJECT_0 || fence->GetCompletedValue() != 1)
        throw std::runtime_error("GPU fence");
    void* data{};
    D3D12_RANGE range{0, 256};
    check(readback->Map(0, &range, &data), "readback");
    UINT matched = 0;
    for (UINT i = 0; i < 64; ++i) {
        const UINT x = i % 8, y = i / 8;
        const bool covered = draw && (!clipped || (x >= 2 && x < 6 && y >= 1 && y < 5));
        const UINT expected = covered ? 0x2468ace0 : seed;
        matched += static_cast<const UINT*>(data)[i] == expected;
    }
    readback->Unmap(0, &none);
    std::fprintf(file, "%s_%s=%u\n", name, clipped ? "clipped" : "full", matched);
    std::fflush(file);
    if (matched != 64)
        throw std::runtime_error("mesh/pixel readback mismatch");
}

int main() {
    FILE* file{};
    if (fopen_s(&file, "result.txt", "wb")) return 2;
    int exit = 0;
    try {
        for (const char* name : {"plain-3", "swizzle-3", "plain-67", "swizzle-67",
                                 "plain-128", "swizzle-128", "plain-0", "swizzle-0",
                                 "plain-129", "swizzle-129", "bad-index"}) {
            const std::string id(name);
            const bool draw = id != "bad-index" && id.find("-0") == std::string::npos
                && id.find("-129") == std::string::npos;
            run(name, draw, false, file);
            run(name, draw, true, file);
        }
    } catch (const std::exception& e) {
        std::fprintf(file, "error=%s\n", e.what());
        exit = 1;
    }
    return fclose(file) ? 2 : exit;
}
