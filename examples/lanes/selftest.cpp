#include "selftest.h"
#include "game.h"
#include "gpu.h"
#include "screen.h"
#include "../../runtime/d3d12/compute_fixture.h"
#include "../../runtime/d3d12/frame_stream.h"
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <vector>
#include "course_shader.h"
#include "gpu_indirect_shader.h"
#include "xbox_shader.h"
#include "raster_shader.h"
#include "reclaim_shader.h"
#include "wave_alloc_shader.h"
#include <algorithm>
#include <memory>

namespace {
using compute_fixture::buffer;
using frame_abi::check;
using frame_abi::ComPtr;
using frame_abi::method;

// The Scarlett release record with ID3D12Device11 selects the queue and
// command-list tables a Series X|S title built with a recent GDK uses.
constexpr std::array<UINT, 18> scarlett_profile{
    0x20004, 0, UINT_MAX, 0, 0, 0, UINT_MAX, UINT_MAX, 0, 1, 0, 0, 0, 0, 0, 0, 0, 0};

// A queue held on a label is released by the provider after five seconds, so a
// scenario that needs that release must already count as failed.
constexpr DWORD deadline_ms = 2000;

// Sentinel written to the wave-alloc UAV so a shader that never stores is
// distinguishable from one that stored the expected value (0).
constexpr UINT kWaveAllocSentinel = 0xdeadbeefu;

// Xbox command-list and queue slots (GDK gen9/d3d12_xs.h).
constexpr size_t slot_copy_memory = 62, slot_write32 = 71, slot_write64 = 72, slot_wait64 = 74,
                 slot_dispatch_flagged = 80, slot_copy_texture = 83, slot_build = 119,
                 slot_emit_sizes = 120, slot_dispatch_mesh = 129, slot_sync_top = 131, slot_sync_bottom = 132;
constexpr size_t slot_compute_root = 33, device_create_compute = 62, device_create_stream = 86;
constexpr size_t slot_bind_heaps = 32, slot_compute_table = 35, queue_wait = 17;
constexpr size_t slot_graphics_root = 34, slot_graphics_table = 36, slot_targets = 50, slot_clear_target = 52;
constexpr size_t slot_execute_indirect = 61, slot_root_constants = 39, slot_root_uav = 45;
constexpr size_t queue_signal = 16, queue_execute_frame = 35, device_place = 44, device_create_list = 73;

using CopyMemory_ = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT64, UINT64, UINT64, UINT);
using Write32 = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT64, UINT, UINT, UINT);
using Write64 = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT64, UINT64, UINT, UINT);
using Wait64 = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT64, D3D12_COMPARISON_FUNC, UINT64, UINT);
using DispatchFlagged = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, UINT, UINT, UINT);
using DispatchMesh = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, UINT, UINT);
using Build = void (WINAPI*)(ID3D12GraphicsCommandList*, const void*, UINT, const void*);
using Sync = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, UINT, UINT64, UINT64);
using SignalQueue = HRESULT (WINAPI*)(ID3D12CommandQueue*, ID3D12Fence*, UINT64);
using ExecuteFrame = void (WINAPI*)(ID3D12CommandQueue*, UINT, ID3D12CommandList* const*, UINT64, UINT);
using CreateComputePipeline = HRESULT (WINAPI*)(ID3D12Device*, const D3D12_COMPUTE_PIPELINE_STATE_DESC*,
                                                UINT, const void*, REFIID, void**);
using CreateStreamPipeline = HRESULT (WINAPI*)(ID3D12Device*, const D3D12_PIPELINE_STATE_STREAM_DESC*,
                                               REFIID, void**);
using SetRoot = void (WINAPI*)(ID3D12GraphicsCommandList*, ID3D12RootSignature*);
using ExecuteIndirect_ = void (WINAPI*)(ID3D12GraphicsCommandList*, ID3D12CommandSignature*, UINT,
                                         ID3D12Resource*, UINT64, ID3D12Resource*, UINT64);
using SetConstants = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, UINT, const void*, UINT);
using SetUav = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, D3D12_GPU_VIRTUAL_ADDRESS);
using BindHeaps = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, ID3D12DescriptorHeap* const*);
using SetTable = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, D3D12_GPU_DESCRIPTOR_HANDLE);
using SetTargets = void (WINAPI*)(ID3D12GraphicsCommandList*, UINT, const D3D12_CPU_DESCRIPTOR_HANDLE*, BOOL,
                                  const D3D12_CPU_DESCRIPTOR_HANDLE*);
using WaitQueue = HRESULT (WINAPI*)(ID3D12CommandQueue*, ID3D12Fence*, UINT64);
using GpuHandle = UINT64 (WINAPI*)(ID3D12DescriptorHeap*);
using EmitSizes = void (WINAPI*)(ID3D12GraphicsCommandList*,
                                 const D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC*, UINT,
                                 const D3D12_GPU_VIRTUAL_ADDRESS*);
using Place = HRESULT (WINAPI*)(ID3D12Device*, UINT64, const D3D12_RESOURCE_DESC*, D3D12_RESOURCE_STATES,
                                const D3D12_CLEAR_VALUE*, REFIID, void**);

// Committed title memory. A Series title addresses the GPU with these pointers.
struct Memory {
    BYTE* base;
    explicit Memory(size_t bytes)
        : base(static_cast<BYTE*>(VirtualAlloc(nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE))) {
        if (!base) throw std::runtime_error("allocate title memory");
    }
    ~Memory() { VirtualFree(base, 0, MEM_RELEASE); }
    Memory(const Memory&) = delete;
    Memory& operator=(const Memory&) = delete;
    UINT64 address(size_t offset = 0) const { return reinterpret_cast<UINT64>(base + offset); }
    UINT64 read64(size_t offset) const { return *reinterpret_cast<volatile const UINT64*>(base + offset); }
};

ComPtr<ID3D12GraphicsCommandList> open_list(ID3D12Device* device, D3D12_COMMAND_LIST_TYPE type,
                                             ID3D12CommandAllocator* allocator) {
    const UINT record[] = {UINT(type), 0, 1, 0};
    ComPtr<ID3D12GraphicsCommandList> list;
    check(method<frame_abi::CreateList>(device, device_create_list)(
              device, record, allocator, nullptr, IID_PPV_ARGS(&list)), "create list");
    check(list->Reset(allocator, nullptr), "open list");
    return list;
}

struct Queue {
    ComPtr<ID3D12CommandQueue> queue;
    ComPtr<ID3D12CommandAllocator> allocator;
    ComPtr<ID3D12GraphicsCommandList> list;
    ComPtr<ID3D12Fence> fence;
    UINT64 submitted = 0;

    Queue(ID3D12Device* device, D3D12_COMMAND_LIST_TYPE type) {
        D3D12_COMMAND_QUEUE_DESC desc{};
        desc.Type = type;
        check(device->CreateCommandQueue(&desc, IID_PPV_ARGS(&queue)), "create queue");
        check(device->CreateCommandAllocator(type, IID_PPV_ARGS(&allocator)), "create allocator");
        list = open_list(device, type, allocator.Get());
        check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&fence)), "create fence");
    }
    void write32(UINT64 address, UINT value, UINT flush = 0) {
        method<Write32>(list.Get(), slot_write32)(list.Get(), address, value, flush, 0);
    }
    void write64(UINT64 address, UINT64 value, UINT flush = 0) {
        method<Write64>(list.Get(), slot_write64)(list.Get(), address, value, flush, 0);
    }
    void wait64(UINT64 address, UINT64 value) {
        method<Wait64>(list.Get(), slot_wait64)(list.Get(), address, D3D12_COMPARISON_FUNC_GREATER_EQUAL, value, 0);
    }
    void close() { check(list->Close(), "close list"); }
    // Reuse the allocator and list once everything submitted has finished.
    void reopen() {
        check(allocator->Reset(), "reset allocator");
        check(list->Reset(allocator.Get(), nullptr), "reopen list");
    }
    void signal() {
        check(method<SignalQueue>(queue.Get(), queue_signal)(queue.Get(), fence.Get(), ++submitted), "signal queue");
    }
    void submit() {
        close();
        ID3D12CommandList* batch[]{list.Get()};
        queue->ExecuteCommandLists(1, batch);
        signal();
    }
    // Wait until everything submitted has finished, or fail.
    void finish(const char* what) {
        HANDLE event = CreateEventW(nullptr, FALSE, FALSE, nullptr);
        if (!event) throw std::runtime_error("create completion event");
        const HRESULT armed = fence->SetEventOnCompletion(submitted, event);
        const DWORD waited = SUCCEEDED(armed) ? WaitForSingleObject(event, deadline_ms) : WAIT_FAILED;
        CloseHandle(event);
        check(armed, "arm completion event");
        if (waited != WAIT_OBJECT_0)
            throw std::runtime_error(std::string(what) + " did not finish within 2 seconds");
    }
};

void expect_value(const Memory& memory, size_t offset, UINT64 value, const char* what) {
    const UINT64 found = memory.read64(offset);
    if (found != value) {
        char message[160];
        std::snprintf(message, sizeof(message), "%s: expected %llu, found %llu", what,
                      static_cast<unsigned long long>(value), static_cast<unsigned long long>(found));
        throw std::runtime_error(message);
    }
}

// Both bottom-of-pipe writes store ordinary values, with or without a flush.
void labels(ID3D12Device* device) {
    Memory memory(65536);
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    direct.write32(memory.address(0), 0x12345678);
    direct.write32(memory.address(8), 7, 1);
    direct.write64(memory.address(16), 0x1122334455667788ull);
    direct.write64(memory.address(24), 42, 0x100);
    direct.submit();
    direct.finish("label writes");
    expect_value(memory, 0, 0x12345678, "32-bit write");
    expect_value(memory, 8, 7, "32-bit write with a flush");
    expect_value(memory, 16, 0x1122334455667788ull, "64-bit write");
    expect_value(memory, 24, 42, "64-bit write with a flush");
}

// A compute queue waits for a label the direct queue writes later, and the
// direct queue then waits for the compute queue's answer.
void cross_queue(ID3D12Device* device) {
    constexpr size_t request = 0, reply = 8, payload = 65536, readback = 2 * 65536;
    constexpr UINT64 expected = 0x123456789abcdef0ull;
    Memory memory(3 * 65536);
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);

    compute.wait64(memory.address(request), 5);
    compute.write64(memory.address(payload), expected);
    compute.write64(memory.address(reply), 6);
    compute.submit();

    // Record the return wait while the compute queue is still waiting for the request.
    direct.wait64(memory.address(reply), 6);
    method<CopyMemory_>(direct.list.Get(), slot_copy_memory)(
        direct.list.Get(), memory.address(readback), memory.address(payload), sizeof(expected), 0);

    ComPtr<ID3D12CommandAllocator> request_allocator;
    check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_DIRECT, IID_PPV_ARGS(&request_allocator)),
          "create request allocator");
    auto request_list = open_list(device, D3D12_COMMAND_LIST_TYPE_DIRECT, request_allocator.Get());
    method<Write64>(request_list.Get(), slot_write64)(request_list.Get(), memory.address(request), 5, 0, 0);
    check(request_list->Close(), "close request list");
    ID3D12CommandList* batch[]{request_list.Get()};
    direct.queue->ExecuteCommandLists(1, batch);
    direct.submit();

    try {
        compute.finish("compute queue waiting on a direct queue label");
        direct.finish("direct queue waiting on the compute reply");
    } catch (const std::runtime_error&) {
        // Let pending GPU work drain before its lists and title memory are released.
        InterlockedExchange64(reinterpret_cast<volatile LONG64*>(memory.base + request), 5);
        InterlockedExchange64(reinterpret_cast<volatile LONG64*>(memory.base + reply), 6);
        compute.finish("drain compute queue after failed wait");
        direct.finish("drain direct queue after failed wait");
        throw;
    }
    expect_value(memory, reply, 6, "compute reply");
    expect_value(memory, readback, expected, "payload copied after the compute reply");
}

// The compute queue submits through ExecuteCommandListsX as well.
void compute_frame(ID3D12Device* device) {
    Memory memory(65536);
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    compute.write64(memory.address(0), 99);
    compute.close();
    ID3D12CommandList* batch[]{compute.list.Get()};
    method<ExecuteFrame>(compute.queue.Get(), queue_execute_frame)(compute.queue.Get(), 1, batch, 1, 3);
    compute.signal();
    compute.finish("compute frame submission");
    expect_value(memory, 0, 99, "compute frame write");
}

// A command that cannot be recorded is left out on its own. The list still
// closes and its fence write, which another queue may need, still happens.
void skipped_command(ID3D12Device* device) {
    Memory memory(65536);
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    auto sync_top = method<Sync>(direct.list.Get(), slot_sync_top);
    auto sync_bottom = method<Sync>(direct.list.Get(), slot_sync_bottom);
    sync_top(direct.list.Get(), 0xc280, 0x20000, 0, 0xffffffffff00ull);
    // The BVH builder passes no description for a build it cannot express.
    method<Build>(direct.list.Get(), slot_build)(direct.list.Get(), nullptr, 0, nullptr);
    sync_bottom(direct.list.Get(), 0xc280, 0x20000, 0, 0xffffffffff00ull);
    direct.write64(memory.address(0), 11);
    direct.submit();
    direct.finish("list with a skipped command");
    expect_value(memory, 0, 11, "write after a skipped command");
}

// CopyMemoryToMemoryX copies from the source to the destination.
void copy_memory(ID3D12Device* device) {
    constexpr size_t bytes = 4096, source = 0, destination = 65536;
    Memory memory(2 * 65536);
    for (size_t i = 0; i < bytes; ++i) {
        memory.base[source + i] = static_cast<BYTE>(i * 31 + 7);
        memory.base[destination + i] = 0xee;
    }
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    method<CopyMemory_>(direct.list.Get(), slot_copy_memory)(
        direct.list.Get(), memory.address(destination), memory.address(source), bytes, 0);
    direct.submit();
    direct.finish("memory copy");
    if (std::memcmp(memory.base + source, memory.base + destination, bytes))
        throw std::runtime_error("memory copy did not reach the destination");
    if (memory.base[source + 1] != 38)
        throw std::runtime_error("memory copy overwrote its source");
}

// Compare a direct queue waiting before and immediately after a buffer copy.
// The compute producer remains unsubmitted during the observation window.
void direct_copy_wait(ID3D12Device* device) {
    constexpr UINT marker = 0x5a17c0de;
    constexpr UINT64 expected = 0x12345678;
    constexpr size_t label = 0, payload = 65536, copied = 2 * 65536;
    bool passed = true;
    for (bool after_copy : {false, true}) {
        Memory memory(3 * 65536);
        std::memset(memory.base + copied, 0xcc, sizeof(expected));
        Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
        const UINT seeds[]{0, marker};
        auto upload = buffer(device, D3D12_HEAP_TYPE_UPLOAD, sizeof(seeds), D3D12_RESOURCE_STATE_GENERIC_READ);
        auto progress = buffer(device, D3D12_HEAP_TYPE_READBACK, sizeof(marker), D3D12_RESOURCE_STATE_COPY_DEST);
        compute_fixture::upload(upload.Get(), seeds, sizeof(seeds));
        auto* list = direct.list.Get();
        list->CopyBufferRegion(progress.Get(), 0, upload.Get(), 0, sizeof(marker));
        direct.submit();
        direct.finish("initialize prefix marker");
        direct.reopen();

        auto read_marker = [&] {
            D3D12_RANGE read{0, sizeof(marker)}, empty{};
            void* mapped{};
            check(progress->Map(0, &read, &mapped), "map prefix marker");
            const UINT value = *static_cast<const volatile UINT*>(mapped);
            progress->Unmap(0, &empty);
            return value;
        };
        if (read_marker() != 0) throw std::runtime_error("bad initial prefix marker");

        compute.write64(memory.address(payload), expected);
        compute.write64(memory.address(label), 1);
        if (!after_copy) direct.wait64(memory.address(label), 1);
        list->CopyBufferRegion(progress.Get(), 0, upload.Get(), sizeof(UINT), sizeof(marker));
        if (after_copy) direct.wait64(memory.address(label), 1);
        method<CopyMemory_>(list, slot_copy_memory)(list, memory.address(copied), memory.address(payload),
                                                   sizeof(expected), 0);
        direct.submit();

        Sleep(250);
        const UINT64 completed = direct.fence->GetCompletedValue();
        const UINT prefix = read_marker();
        const UINT64 copied_before = memory.read64(copied);
        compute.submit();
        compute.finish("direct-copy-wait producer");
        direct.finish("direct-copy-wait consumer");
        if (completed == UINT64_MAX) throw std::runtime_error("device removed during direct-copy-wait");
        expect_value(memory, label, 1, "producer label");
        expect_value(memory, payload, expected, "producer payload");
        if (read_marker() != marker) throw std::runtime_error("first buffer copy missing");
        const bool early = completed >= direct.submitted;
        const UINT64 value = memory.read64(copied);
        std::fprintf(stderr, "direct-copy-wait position=%s prefix_before=0x%08x copied_before=0x%016llx completed_before_producer=%s cpu=0x%08llx\n",
                     after_copy ? "after-copy" : "list-start", prefix, static_cast<unsigned long long>(copied_before), early ? "yes" : "no",
                     static_cast<unsigned long long>(value));
        passed &= prefix == (after_copy ? marker : 0u) && copied_before == 0xccccccccccccccccull && !early && value == expected;
    }
    if (!passed) throw std::runtime_error("direct queue copy ordering or readback mismatch");
}

// The compute queue waits for a label, then reads a guest texture the direct
// queue writes before setting that label. A provider that makes the writer wait
// for the earlier submitted reader deadlocks the two queues.
void shared_texture(ID3D12Device* device) {
    constexpr UINT width = 256, height = 256, pitch = 256;
    constexpr UINT64 bytes = UINT64(pitch) * height;
    Memory memory(4 * 65536);
    Memory label(65536);
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R8_UNORM;
    desc.Layout = static_cast<D3D12_TEXTURE_LAYOUT>(0x105);
    ComPtr<ID3D12Resource> texture;
    check(method<Place>(device, device_place)(device, memory.address(), &desc, D3D12_RESOURCE_STATE_COMMON,
                                              nullptr, IID_PPV_ARGS(&texture)), "place guest texture");
    auto upload = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    std::vector<BYTE> expected(bytes);
    for (size_t i = 0; i < bytes; ++i) expected[i] = static_cast<BYTE>(i * 13 + i / pitch);
    compute_fixture::upload(upload.Get(), expected.data(), bytes);

    D3D12_TEXTURE_COPY_LOCATION image{}, linear{};
    image.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    image.pResource = texture.Get();
    linear.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    linear.PlacedFootprint.Footprint = {DXGI_FORMAT_R8_UNORM, width, height, 1, pitch};

    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    compute.wait64(label.address(), 1);
    linear.pResource = readback.Get();
    method<frame_abi::Copy>(compute.list.Get(), slot_copy_texture)(
        compute.list.Get(), &linear, 0, 0, 0, &image, nullptr, 0);
    compute.submit();
    linear.pResource = upload.Get();
    method<frame_abi::Copy>(direct.list.Get(), slot_copy_texture)(
        direct.list.Get(), &image, 0, 0, 0, &linear, nullptr, 0);
    direct.write64(label.address(), 1);
    direct.submit();
    direct.finish("direct queue writing a texture a waiting queue reads");
    compute.finish("compute queue reading the texture");

    D3D12_RANGE range{0, static_cast<SIZE_T>(bytes)}, empty{};
    void* mapped{};
    check(readback->Map(0, &range, &mapped), "map readback");
    const bool matched = std::memcmp(mapped, expected.data(), bytes) == 0;
    readback->Unmap(0, &empty);
    if (!matched) throw std::runtime_error("the reader did not see the writer's texels");
}

// A console shader reaches the GPU through the translation cache. The provider
// substitutes an empty pipeline when no translation exists, so `verify` also
// checks that it reports loading the translated one.
void xbox_shader_scenario(ID3D12Device* device) {
    ComPtr<ID3D12RootSignature> root;
    D3D12_ROOT_SIGNATURE_DESC signature{};
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize empty root signature");
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create empty root signature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
    shader.pRootSignature = root.Get();
    shader.CS = {xbox_shader, sizeof(xbox_shader)};
    ComPtr<ID3D12PipelineState> pipeline;
    check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                       IID_PPV_ARGS(&pipeline)),
          "create pipeline from a console shader");
    Memory memory(65536);
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    method<SetRoot>(compute.list.Get(), slot_compute_root)(compute.list.Get(), root.Get());
    compute.list->SetPipelineState(pipeline.Get());
    compute.list->Dispatch(1, 1, 1);
    compute.write64(memory.address(0), 3);
    compute.submit();
    compute.finish("dispatch of the console shader");
    expect_value(memory, 0, 3, "write after the dispatch");
}

// A console program that sums across its 64-lane waves. A host without such
// waves needs the translation made for 32 lanes; without either translation the
// provider substitutes an empty pipeline and nothing is written.
void wave64_shader_scenario(ID3D12Device* device) {
    constexpr UINT words = static_cast<UINT>(std::size(wave_shader_expected));
    constexpr UINT64 bytes = words * sizeof(UINT);
    D3D12_DESCRIPTOR_RANGE range{D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 0};
    D3D12_ROOT_PARAMETER table{};
    table.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
    table.DescriptorTable = {1, &range};
    D3D12_ROOT_SIGNATURE_DESC signature{1, &table, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize wave root signature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create wave root signature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
    shader.pRootSignature = root.Get();
    shader.CS = {wave_shader, sizeof(wave_shader)};
    ComPtr<ID3D12PipelineState> pipeline;
    check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                       IID_PPV_ARGS(&pipeline)),
          "create pipeline from a wave64 console shader");

    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    ComPtr<ID3D12DescriptorHeap> heap;
    check(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)), "create wave view heap");
    const D3D12_CPU_DESCRIPTOR_HANDLE cpu{method<frame_abi::CpuHandle>(heap.Get(), 9)(heap.Get())};
    const D3D12_GPU_DESCRIPTOR_HANDLE gpu{method<GpuHandle>(heap.Get(), 10)(heap.Get())};

    auto seed = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                         D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    const std::vector<UINT> unwritten(words, 0xdeadbeefu);
    compute_fixture::upload(seed.Get(), unwritten.data(), size_t(bytes));
    D3D12_UNORDERED_ACCESS_VIEW_DESC view{};
    view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
    view.Format = DXGI_FORMAT_R32_TYPELESS;
    view.Buffer.NumElements = words;
    view.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
    device->CreateUnorderedAccessView(output.Get(), nullptr, &view, cpu);

    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    ID3D12GraphicsCommandList* list = direct.list.Get();
    list->CopyBufferRegion(output.Get(), 0, seed.Get(), 0, bytes);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    ID3D12DescriptorHeap* heaps[]{heap.Get()};
    method<BindHeaps>(list, slot_bind_heaps)(list, 1, heaps);
    method<SetRoot>(list, slot_compute_root)(list, root.Get());
    list->SetPipelineState(pipeline.Get());
    method<SetTable>(list, slot_compute_table)(list, 0, gpu);
    list->Dispatch(1, 1, 1);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(readback.Get(), 0, output.Get(), 0, bytes);
    direct.submit();
    direct.finish("dispatch of the wave64 console shader");

    D3D12_RANGE read{0, SIZE_T(bytes)}, empty{};
    void* mapped = nullptr;
    check(readback->Map(0, &read, &mapped), "map wave readback");
    std::vector<UINT> got(words);
    std::memcpy(got.data(), mapped, size_t(bytes));
    readback->Unmap(0, &empty);
    for (UINT i = 0; i < words; ++i) {
        if (got[i] != wave_shader_expected[i]) {
            char message[128];
            std::snprintf(message, sizeof(message), "wave sum mismatch at thread %u: expected %u, found %u",
                          i, wave_shader_expected[i], got[i]);
            throw std::runtime_error(message);
        }
    }
}

// A root with two SRV tables (t0, space0; t0 to t3, space1) and one root
// constant (b500), optionally with static samplers, for the console raster programs.
ComPtr<ID3D12RootSignature> two_table_root(ID3D12Device* device, const D3D12_STATIC_SAMPLER_DESC* samplers,
                                           UINT count = 1) {
    D3D12_DESCRIPTOR_RANGE1 ranges[]{
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 4, 0, 1, D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE, 0}};
    D3D12_ROOT_PARAMETER1 params[3]{};
    for (UINT i = 0; i < 2; ++i) {
        params[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[i].DescriptorTable = {1, &ranges[i]};
    }
    params[2].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[2].Constants = {500, 0, 1};
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC signature{D3D_ROOT_SIGNATURE_VERSION_1_1};
    signature.Desc_1_1 = {3, params, samplers ? count : 0u, samplers, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeVersionedRootSignature(&signature, &serialized, nullptr),
          "serialize two-table root signature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create two-table root signature");
    return root;
}

// The stream parts a console raster draw shares, for a vertex or a mesh pipeline.
template <class Stream>
ComPtr<ID3D12PipelineState> console_pipeline(ID3D12Device* device, Stream& parts, ID3D12RootSignature* root,
                                             D3D12_SHADER_BYTECODE pixel, DXGI_FORMAT format) {
    parts.root.payload = root;
    parts.ps.payload = pixel;
    parts.raster.payload.FillMode = D3D12_FILL_MODE_SOLID;
    parts.raster.payload.CullMode = D3D12_CULL_MODE_NONE;
    parts.raster.payload.DepthClipEnable = TRUE;
    parts.blend.payload.RenderTarget[0].RenderTargetWriteMask = D3D12_COLOR_WRITE_ENABLE_ALL;
    parts.dsv.payload = DXGI_FORMAT_UNKNOWN;
    parts.formats.payload.NumRenderTargets = 1;
    parts.formats.payload.RTFormats[0] = format;
    D3D12_PIPELINE_STATE_STREAM_DESC stream{sizeof(parts), &parts};
    ComPtr<ID3D12PipelineState> pipeline;
    check(method<CreateStreamPipeline>(device, device_create_stream)(device, &stream, IID_PPV_ARGS(&pipeline)),
          "create pipeline from console raster programs");
    return pipeline;
}

// Draws a fullscreen triangle with the console vertex program, or with `mesh`
// when it is given, and `pixel` under `root`, into a 16x16 target of 32-bit
// `format` cleared to zero. Table N is bound at view `tables[N]` of `view_heap`.
// Returns the texels, row by row.
std::vector<UINT> draw_console_raster(ID3D12Device* device, ID3D12RootSignature* root, D3D12_SHADER_BYTECODE pixel,
                                      DXGI_FORMAT format, ID3D12DescriptorHeap* view_heap,
                                      std::initializer_list<UINT> tables, D3D12_SHADER_BYTECODE mesh = {}) {
    ComPtr<ID3D12PipelineState> pipeline;
    if (mesh.BytecodeLength) {
        frame_abi::MeshPipelineStream parts;
        parts.ms.payload = mesh;
        pipeline = console_pipeline(device, parts, root, pixel, format);
    } else {
        frame_abi::RasterPipelineStream parts;
        parts.vs.payload = {raster_vertex, sizeof(raster_vertex)};
        pipeline = console_pipeline(device, parts, root, pixel, format);
    }

    constexpr UINT side = 16, pitch = 256;
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const UINT64 gpu_base = method<GpuHandle>(view_heap, 10)(view_heap);
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC target_desc{};
    target_desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    target_desc.Width = target_desc.Height = side;
    target_desc.DepthOrArraySize = target_desc.MipLevels = 1;
    target_desc.SampleDesc.Count = 1;
    target_desc.Format = format;
    target_desc.Flags = D3D12_RESOURCE_FLAG_ALLOW_RENDER_TARGET;
    ComPtr<ID3D12Resource> target;
    check(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &target_desc, D3D12_RESOURCE_STATE_RENDER_TARGET,
                                          nullptr, IID_PPV_ARGS(&target)), "create render target");
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, side * pitch, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_DESCRIPTOR_HEAP_DESC target_heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_RTV, 1, D3D12_DESCRIPTOR_HEAP_FLAG_NONE, 0};
    ComPtr<ID3D12DescriptorHeap> target_heap;
    check(device->CreateDescriptorHeap(&target_heap_desc, IID_PPV_ARGS(&target_heap)), "create target heap");
    const D3D12_CPU_DESCRIPTOR_HANDLE rtv{method<frame_abi::CpuHandle>(target_heap.Get(), 9)(target_heap.Get())};
    device->CreateRenderTargetView(target.Get(), nullptr, rtv);

    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    auto* list = direct.list.Get();
    constexpr float clear[4]{};
    method<frame_abi::Clear>(list, slot_clear_target)(list, rtv, clear, 0, nullptr);
    method<SetRoot>(list, slot_graphics_root)(list, root);
    ID3D12DescriptorHeap* bound[]{view_heap};
    method<BindHeaps>(list, slot_bind_heaps)(list, 1, bound);
    UINT parameter = 0;
    for (const UINT view : tables)
        method<SetTable>(list, slot_graphics_table)(list, parameter++, {gpu_base + UINT64(view) * stride});
    method<SetTargets>(list, slot_targets)(list, 1, &rtv, FALSE, nullptr);
    const D3D12_VIEWPORT viewport{0, 0, float(side), float(side), 0, 1};
    const D3D12_RECT scissor{0, 0, LONG(side), LONG(side)};
    list->RSSetViewports(1, &viewport);
    list->RSSetScissorRects(1, &scissor);
    list->SetPipelineState(pipeline.Get());
    if (mesh.BytecodeLength) {
        method<DispatchMesh>(list, slot_dispatch_mesh)(list, 1, 1, 1);
    } else {
        list->IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
        list->DrawInstanced(3, 1, 0, 0);
    }
    compute_fixture::transition(list, target.Get(), D3D12_RESOURCE_STATE_RENDER_TARGET,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    D3D12_TEXTURE_COPY_LOCATION from{}, to{};
    from.pResource = target.Get();
    from.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    to.pResource = readback.Get();
    to.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    to.PlacedFootprint = {0, {format, side, side, 1, pitch}};
    method<frame_abi::Copy>(list, slot_copy_texture)(list, &to, 0, 0, 0, &from, nullptr, 0);
    direct.submit();
    direct.finish("draw with two root tables");

    std::vector<UINT> rows(side * pitch / 4), texels(side * side);
    D3D12_RANGE range{0, side * pitch}, empty{};
    void* mapped{};
    check(readback->Map(0, &range, &mapped), "map render target readback");
    std::memcpy(rows.data(), mapped, side * pitch);
    readback->Unmap(0, &empty);
    for (UINT y = 0; y < side; ++y)
        std::copy_n(&rows[y * pitch / 4], side, &texels[y * side]);
    return texels;
}

// A console pixel program reads a descriptor through the second of two root
// tables. Its FSSM records and PSV binding name it t3, space2 at slot 2 of
// table 1, which the title's root declares as t2, space1. The provider must
// build the root the program was compiled against (`raster_fixture.py`) and
// draw with it; under the title's root the pipeline becomes a stand-in and the
// target keeps its clear value.
void raster_tables(ID3D12Device* device) {
    auto root = two_table_root(device, nullptr);

    // Sixteen raw views. Table 0 starts at view 0 and table 1 at view 8, so the
    // program's descriptor is view 10. Every other view holds a decoy naming it.
    constexpr UINT views = 16, view_bytes = 256, side = 16;
    std::vector<UINT> words(views * view_bytes / 4);
    for (UINT i = 0; i < views; ++i) words[i * view_bytes / 4] = i == 10 ? raster_expected : raster_decoy | i;
    auto data = buffer(device, D3D12_HEAP_TYPE_UPLOAD, words.size() * 4, D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(data.Get(), words.data(), words.size() * 4);
    D3D12_DESCRIPTOR_HEAP_DESC view_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, views,
                                         D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 1};
    ComPtr<ID3D12DescriptorHeap> view_heap;
    check(device->CreateDescriptorHeap(&view_desc, IID_PPV_ARGS(&view_heap)), "create view heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const SIZE_T cpu_base = method<frame_abi::CpuHandle>(view_heap.Get(), 9)(view_heap.Get());
    for (UINT i = 0; i < views; ++i) {
        D3D12_SHADER_RESOURCE_VIEW_DESC srv{};
        srv.Format = DXGI_FORMAT_R32_TYPELESS;
        srv.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Buffer = {UINT64(i) * view_bytes / 4, view_bytes / 4, 0, D3D12_BUFFER_SRV_FLAG_RAW};
        device->CreateShaderResourceView(data.Get(), &srv, {cpu_base + SIZE_T(i) * stride});
    }

    const auto texels = draw_console_raster(device, root.Get(), {raster_pixel, sizeof(raster_pixel)},
                                            DXGI_FORMAT_R32_UINT, view_heap.Get(), {0, 8});
    for (UINT i = 0; i < texels.size(); ++i) {
        const UINT found = texels[i], x = i % side, y = i / side;
        if (found == raster_expected) continue;
        char message[160];
        if (!found)
            std::snprintf(message, sizeof(message), "texel %u,%u is 0: a stand-in pipeline or a skipped draw", x, y);
        else if ((found & ~0xffu) == raster_decoy)
            std::snprintf(message, sizeof(message), "texel %u,%u read view %u, not table 1 slot 2 (view 10)", x, y,
                          found & 0xffu);
        else
            std::snprintf(message, sizeof(message), "texel %u,%u: expected %#x, found %#x", x, y,
                          raster_expected, found);
        throw std::runtime_error(message);
    }
}

// A point-filtered, clamped static sampler at register `slot`, space0.
D3D12_STATIC_SAMPLER_DESC point_sampler(UINT slot) {
    D3D12_STATIC_SAMPLER_DESC point{};
    point.Filter = D3D12_FILTER_MIN_MAG_MIP_POINT;
    point.AddressU = point.AddressV = point.AddressW = D3D12_TEXTURE_ADDRESS_MODE_CLAMP;
    point.MaxAnisotropy = 1;
    point.ComparisonFunc = D3D12_COMPARISON_FUNC_NEVER;
    point.BorderColor = D3D12_STATIC_BORDER_COLOR_TRANSPARENT_BLACK;
    point.MaxLOD = 12;
    point.ShaderRegister = slot;
    point.ShaderVisibility = D3D12_SHADER_VISIBILITY_ALL;
    return point;
}

// Draws `pixel` (and `mesh`, when given) under `root` with the 2x1 texture the
// sampler programs read, sampled a quarter of the way between its texels, and
// requires every texel to hold the linear-filtered value.
void draw_linear_sample(ID3D12Device* device, ID3D12RootSignature* root, D3D12_SHADER_BYTECODE pixel,
                        D3D12_SHADER_BYTECODE mesh = {}) {
    D3D12_HEAP_PROPERTIES heap{};
    heap.Type = D3D12_HEAP_TYPE_DEFAULT;
    heap.CreationNodeMask = heap.VisibleNodeMask = 1;
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = std::size(raster_sampler_texels);
    desc.Height = desc.DepthOrArraySize = desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R32_FLOAT;
    ComPtr<ID3D12Resource> texture;
    check(device->CreateCommittedResource(&heap, D3D12_HEAP_FLAG_NONE, &desc, D3D12_RESOURCE_STATE_COPY_DEST,
                                          nullptr, IID_PPV_ARGS(&texture)), "create sampled texture");
    constexpr UINT pitch = 256;
    auto upload = buffer(device, D3D12_HEAP_TYPE_UPLOAD, pitch, D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(upload.Get(), raster_sampler_texels, sizeof(raster_sampler_texels));
    {
        Queue copy(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        D3D12_TEXTURE_COPY_LOCATION to{}, from{};
        to.pResource = texture.Get();
        to.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
        from.pResource = upload.Get();
        from.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
        from.PlacedFootprint = {0, {DXGI_FORMAT_R32_FLOAT, UINT(desc.Width), 1, 1, pitch}};
        method<frame_abi::Copy>(copy.list.Get(), slot_copy_texture)(copy.list.Get(), &to, 0, 0, 0, &from, nullptr, 0);
        compute_fixture::transition(copy.list.Get(), texture.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                    D3D12_RESOURCE_STATE_PIXEL_SHADER_RESOURCE);
        copy.submit();
        copy.finish("upload sampled texture");
    }

    // Table 0 at view 0 and table 1 at view 8, each with room for the ranges
    // the root declares: the texture is view 8, the other views are null.
    constexpr UINT views = 16;
    D3D12_DESCRIPTOR_HEAP_DESC view_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, views,
                                         D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 1};
    ComPtr<ID3D12DescriptorHeap> view_heap;
    check(device->CreateDescriptorHeap(&view_desc, IID_PPV_ARGS(&view_heap)), "create view heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const SIZE_T cpu_base = method<frame_abi::CpuHandle>(view_heap.Get(), 9)(view_heap.Get());
    D3D12_SHADER_RESOURCE_VIEW_DESC srv{};
    srv.Format = DXGI_FORMAT_R32_FLOAT;
    srv.ViewDimension = D3D12_SRV_DIMENSION_TEXTURE2D;
    srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
    srv.Texture2D.MipLevels = 1;
    for (UINT i = 0; i < views; ++i)
        device->CreateShaderResourceView(i == 8 ? texture.Get() : nullptr, &srv, {cpu_base + SIZE_T(i) * stride});

    const auto texels = draw_console_raster(device, root, pixel, DXGI_FORMAT_R32_FLOAT, view_heap.Get(), {0, 8}, mesh);
    for (UINT i = 0; i < texels.size(); ++i) {
        float found;
        std::memcpy(&found, &texels[i], sizeof(found));
        if (found == raster_sampler_linear) continue;
        char message[200];
        if (!texels[i])
            std::snprintf(message, sizeof(message), "texel %u,%u is 0: a stand-in pipeline or a skipped draw", i % 16, i / 16);
        else if (found == raster_sampler_point)
            std::snprintf(message, sizeof(message),
                          "texel %u,%u is %g: point-filtered by the root's s0, not the program's literal linear state",
                          i % 16, i / 16, found);
        else
            std::snprintf(message, sizeof(message), "texel %u,%u: expected %g, found %g", i % 16, i / 16,
                          raster_sampler_linear, found);
        throw std::runtime_error(message);
    }
}

// A console pixel program samples a texture with a linear clamped sampler it
// builds from literal words, as the console compiles a static sampler into the
// program. It declares that sampler as s0, where the title's root has a
// point-filtered static sampler in another state. The console samples with the
// literal state, so the provider must give that state a static sampler of its
// own (`inline_sampler::unmatched`). Bound to the root's s0, the draw reads the
// point-filtered value; a refused translation leaves a stand-in and the clear value.
void raster_samplers(ID3D12Device* device) {
    const auto point = point_sampler(0);
    auto root = two_table_root(device, &point);
    draw_linear_sample(device, root.Get(), {raster_sampler_pixel, sizeof(raster_sampler_pixel)});
}

// The same pixel program in a mesh pipeline. It declares s0 and s1, which the
// title's root has as point-filtered static samplers, so no declared register
// can stand in for the literal linear state and its translation against that
// root is refused. The mesh program's records name b501 where the root has b500,
// so the provider builds the pipeline a mesh root of its own; it must add the
// literal state to that root as a private static sampler and translate the
// pixel program against it. Without that the provider falls back to the pixel
// program's root-less translation, whose private sampler the root lacks: the
// pipeline becomes a stand-in and the target keeps its clear value. The mesh
// program is translated by the same runtime shader worker.
void mesh_samplers(ID3D12Device* device) {
    const D3D12_STATIC_SAMPLER_DESC points[]{point_sampler(0), point_sampler(1)};
    auto root = two_table_root(device, points, UINT(std::size(points)));
    draw_linear_sample(device, root.Get(), {mesh_sampler_pixel, sizeof(mesh_sampler_pixel)},
                       {mesh_sampler_mesh, sizeof(mesh_sampler_mesh)});
}

// The `raster-samplers` pixel program, declaring only s0, in a mesh pipeline
// under a root whose s0 is point-filtered. The translation can bind the literal
// state to the one declared register, whatever state the root gives it.
void mesh_sampler_single(ID3D12Device* device) {
    const auto point = point_sampler(0);
    auto root = two_table_root(device, &point);
    draw_linear_sample(device, root.Get(), {raster_sampler_pixel, sizeof(raster_sampler_pixel)},
                       {mesh_sampler_mesh, sizeof(mesh_sampler_mesh)});
}


// A console shader whose wave32 masked DPP exclusive prefix the translator must
// lower to `WavePrefixSum`. The old translator rejects it with "unrecognized
// masked DPP prologue", so this scenario fails without the wave-alloc lowering.
// The shader stores one word per lane to the UAV (the exclusive prefix of a
// wave of ones is the lane id); the scenario reads back every word and requires
// it to be the expected value, so a numerical miscompile of the lowering is
// caught here and no inter-lane store order can hide a wrong lane.
void wave_alloc_scenario(ID3D12Device* device) {
    constexpr size_t wave_alloc_words = 32;
    D3D12_ROOT_PARAMETER params[1]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    params[0].Descriptor = {0, 0};
    D3D12_ROOT_SIGNATURE_DESC signature{};
    signature.NumParameters = 1;
    signature.pParameters = params;
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize wave-alloc root signature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create wave-alloc root signature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
    shader.pRootSignature = root.Get();
    shader.CS = {wave_alloc_shader, sizeof(wave_alloc_shader)};
    ComPtr<ID3D12PipelineState> pipeline;
    check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                       IID_PPV_ARGS(&pipeline)),
          "create pipeline from the wave-alloc console shader");
    auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, wave_alloc_words * sizeof(UINT),
                         D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    // Seed the UAV with a sentinel so a missing store is distinguishable from the
    // expected values (0..31) in the readback.
    auto sentinel = buffer(device, D3D12_HEAP_TYPE_UPLOAD, wave_alloc_words * sizeof(UINT),
                           D3D12_RESOURCE_STATE_GENERIC_READ);
    const std::vector<UINT> fill(wave_alloc_words, kWaveAllocSentinel);
    compute_fixture::upload(sentinel.Get(), fill.data(), fill.size() * sizeof(UINT));
    auto proof = buffer(device, D3D12_HEAP_TYPE_READBACK, wave_alloc_words * sizeof(UINT),
                        D3D12_RESOURCE_STATE_COPY_DEST);
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    auto* list = compute.list.Get();
    list->CopyBufferRegion(output.Get(), 0, sentinel.Get(), 0, wave_alloc_words * sizeof(UINT));
    method<SetRoot>(list, slot_compute_root)(list, root.Get());
    list->SetPipelineState(pipeline.Get());
    method<SetUav>(list, slot_root_uav)(list, 0, output->GetGPUVirtualAddress());
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    list->Dispatch(1, 1, 1);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(proof.Get(), 0, output.Get(), 0, wave_alloc_words * sizeof(UINT));
    compute.submit();
    compute.finish("dispatch of the wave-alloc console shader");
    D3D12_RANGE range{0, wave_alloc_words * sizeof(UINT)}, empty{};
    void* mapped{};
    check(proof->Map(0, &range, &mapped), "map wave-alloc proof");
    const UINT* rows = static_cast<const UINT*>(mapped);
    for (size_t i = 0; i < wave_alloc_words; ++i) {
        if (rows[i] != static_cast<UINT>(i))
            throw std::runtime_error("wave-alloc shader stored the wrong prefix result at lane " + std::to_string(i) + " (or stored nothing)");
    }
    proof->Unmap(0, &empty);
}

// Two root-less console compute programs under a root with three descriptor
// tables, which the provider cannot reflect. Each program's FSSM record and PSV
// binding name its UAV u3, space2; the root declares that descriptor, slot 2 of
// table 1, as u2, space1 (`shader_fixture.py`). The first program must be
// translated against this root and write its word to view 10. The second stores
// through slot 5, past the table, so nothing can translate it for this root and
// it gets a stand-in that writes nothing. A translation made without the root
// cannot be bound, so falling back to one leaves view 10 with its decoy.
void compute_root(ID3D12Device* device) {
    D3D12_DESCRIPTOR_RANGE ranges[]{
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 4, 0, 1, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 3, 0}};
    D3D12_ROOT_PARAMETER params[3]{};
    for (UINT i = 0; i < 3; ++i) {
        params[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[i].DescriptorTable = {1, &ranges[i]};
    }
    D3D12_ROOT_SIGNATURE_DESC signature{3, params, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize three-table root signature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create three-table root signature");
    auto create = [&](const unsigned char* code, size_t size, ComPtr<ID3D12PipelineState>& pipeline) {
        D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
        shader.pRootSignature = root.Get();
        shader.CS = {code, size};
        return method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                            IID_PPV_ARGS(&pipeline));
    };
    ComPtr<ID3D12PipelineState> pipeline, refused;
    check(create(root_shader, sizeof(root_shader), pipeline), "create pipeline from a root-less console program");
    // With stand-ins disabled the provider fails this creation instead.
    const bool refused_created = SUCCEEDED(create(root_refused_shader, sizeof(root_refused_shader), refused));

    // Sixteen raw views of one buffer. Table 0 starts at view 0, table 1 at view
    // 8 and table 2 at view 12, so the first program's descriptor is view 10.
    // Every view starts with a decoy naming it.
    constexpr UINT views = 16, view_words = 64, decoy = 0xD0000000;
    constexpr UINT64 bytes = views * view_words * sizeof(UINT);
    std::vector<UINT> words(views * view_words);
    for (UINT i = 0; i < views; ++i) words[i * view_words] = decoy | i;
    auto seed = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(seed.Get(), words.data(), size_t(bytes));
    auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                         D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, views,
                                         D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    ComPtr<ID3D12DescriptorHeap> heap;
    check(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)), "create root view heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const SIZE_T cpu_base = method<frame_abi::CpuHandle>(heap.Get(), 9)(heap.Get());
    const UINT64 gpu_base = method<GpuHandle>(heap.Get(), 10)(heap.Get());
    for (UINT i = 0; i < views; ++i) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC view{};
        view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        view.Format = DXGI_FORMAT_R32_TYPELESS;
        view.Buffer.FirstElement = UINT64(i) * view_words;
        view.Buffer.NumElements = view_words;
        view.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
        device->CreateUnorderedAccessView(output.Get(), nullptr, &view, {cpu_base + SIZE_T(i) * stride});
    }

    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    ID3D12GraphicsCommandList* list = direct.list.Get();
    list->CopyBufferRegion(output.Get(), 0, seed.Get(), 0, bytes);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    ID3D12DescriptorHeap* heaps[]{heap.Get()};
    method<BindHeaps>(list, slot_bind_heaps)(list, 1, heaps);
    method<SetRoot>(list, slot_compute_root)(list, root.Get());
    constexpr UINT first_view[]{0, 8, 12};
    for (UINT i = 0; i < 3; ++i)
        method<SetTable>(list, slot_compute_table)(list, i, {gpu_base + UINT64(first_view[i]) * stride});
    if (refused_created) {
        list->SetPipelineState(refused.Get());
        list->Dispatch(1, 1, 1);
    }
    list->SetPipelineState(pipeline.Get());
    list->Dispatch(1, 1, 1);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(readback.Get(), 0, output.Get(), 0, bytes);
    direct.submit();
    direct.finish("dispatch under a three-table root");

    D3D12_RANGE read{0, SIZE_T(bytes)}, empty{};
    void* mapped = nullptr;
    check(readback->Map(0, &read, &mapped), "map root readback");
    std::vector<UINT> got(views * view_words);
    std::memcpy(got.data(), mapped, size_t(bytes));
    readback->Unmap(0, &empty);
    for (UINT i = 0; i < views; ++i) {
        const UINT found = got[i * view_words];
        if (found == (i == 10 ? root_shader_expected : decoy | i)) continue;
        char message[160];
        if (i == 10 && found == (decoy | i))
            std::snprintf(message, sizeof(message), "view 10 kept its decoy: a stand-in pipeline or a skipped dispatch");
        else if (found == root_shader_expected)
            std::snprintf(message, sizeof(message), "view %u was written, not table 1 slot 2 (view 10)", i);
        else
            std::snprintf(message, sizeof(message), "view %u: expected %#x, found %#x", i,
                          i == 10 ? root_shader_expected : decoy | i, found);
        throw std::runtime_error(message);
    }
}

// Two stores under a two-table root the provider reflects only in part, with
// descriptor paging active (`shader_fixture.py`).
void compute_root_recovery(ID3D12Device* device) {
    D3D12_DESCRIPTOR_RANGE1 ranges[]{
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE, 0},
        {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 3, D3D12_DESCRIPTOR_RANGE_FLAG_DESCRIPTORS_VOLATILE, 0}};
    D3D12_ROOT_PARAMETER1 params[2]{};
    for (UINT i = 0; i < 2; ++i) {
        params[i].ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        params[i].DescriptorTable = {1, &ranges[i]};
    }
    D3D12_VERSIONED_ROOT_SIGNATURE_DESC signature{};
    signature.Version = D3D_ROOT_SIGNATURE_VERSION_1_1;
    signature.Desc_1_1 = {2, params, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeVersionedRootSignature(&signature, &serialized, nullptr), "serialize extended root");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create two-table root signature");
    auto create = [&](const unsigned char* code, size_t size, ComPtr<ID3D12PipelineState>& pipeline) {
        D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
        shader.pRootSignature = root.Get();
        shader.CS = {code, size};
        return method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                            IID_PPV_ARGS(&pipeline));
    };
    ComPtr<ID3D12PipelineState> pipeline;
    check(create(recovered_root_shader, sizeof(recovered_root_shader), pipeline), "create partially reflected pipeline");
    // Table 0 starts at view 0, table 1 at view 8; the appended slot is view 10.
    constexpr UINT views = 16, view_words = 64, decoy = 0xD0000000;
    constexpr UINT64 bytes = views * view_words * sizeof(UINT);
    std::vector<UINT> words(views * view_words);
    for (UINT i = 0; i < views; ++i) words[i * view_words] = decoy | i;
    auto seed = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(seed.Get(), words.data(), size_t(bytes));
    auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                         D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, 128u,
                                         D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 0};
    ComPtr<ID3D12DescriptorHeap> heap;
    check(device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&heap)), "create root view heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    const SIZE_T cpu_base = method<frame_abi::CpuHandle>(heap.Get(), 9)(heap.Get());
    const UINT64 gpu_base = method<GpuHandle>(heap.Get(), 10)(heap.Get());
    const UINT base_view = 80;
    for (UINT i = 0; i < views; ++i) {
        D3D12_UNORDERED_ACCESS_VIEW_DESC view{};
        view.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        view.Format = DXGI_FORMAT_R32_TYPELESS;
        view.Buffer.FirstElement = UINT64(i) * view_words;
        view.Buffer.NumElements = view_words;
        view.Buffer.Flags = D3D12_BUFFER_UAV_FLAG_RAW;
        device->CreateUnorderedAccessView(output.Get(), nullptr, &view, {cpu_base + SIZE_T(base_view + i) * stride});
    }

    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    ID3D12GraphicsCommandList* list = direct.list.Get();
    list->CopyBufferRegion(output.Get(), 0, seed.Get(), 0, bytes);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    ID3D12DescriptorHeap* heaps[]{heap.Get()};
    method<BindHeaps>(list, slot_bind_heaps)(list, 1, heaps);
    method<SetRoot>(list, slot_compute_root)(list, root.Get());
    constexpr UINT first_view[]{0, 8};
    for (UINT i = 0; i < 2; ++i)
        method<SetTable>(list, slot_compute_table)(list, i, {gpu_base + UINT64(base_view + first_view[i]) * stride});
    list->SetPipelineState(pipeline.Get());
    list->Dispatch(1, 1, 1);
    compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(readback.Get(), 0, output.Get(), 0, bytes);
    direct.submit();
    direct.finish("dispatch under a two-table root");

    D3D12_RANGE read{0, SIZE_T(bytes)}, empty{};
    void* mapped = nullptr;
    check(readback->Map(0, &read, &mapped), "map root readback");
    std::vector<UINT> got(views * view_words);
    std::memcpy(got.data(), mapped, size_t(bytes));
    readback->Unmap(0, &empty);
    {
        for (UINT i = 0; i < views; ++i)
            for (UINT j = 1; j < view_words; ++j)
                if (got[i * view_words + j] != 0)
                    throw std::runtime_error("recovery modified a guard word");
    }
    for (UINT i = 0; i < views; ++i) {
        const UINT found = got[i * view_words];
        const bool written = i == 0 || i == 10;
        if (found == (written ? root_shader_expected : decoy | i)) continue;
        char message[160];
        if (written && found == (decoy | i))
            std::snprintf(message, sizeof(message), "view %u kept its decoy: a stand-in pipeline or a skipped dispatch", i);
        else if (found == root_shader_expected)
            std::snprintf(message, sizeof(message), "view %u was written unexpectedly", i);
        else
            std::snprintf(message, sizeof(message), "view %u: expected %#x, found %#x", i,
                          written ? root_shader_expected : decoy | i, found);
        throw std::runtime_error(message);
    }
}

ComPtr<ID3D12PipelineState> compute_pipeline(ID3D12Device* device, ComPtr<ID3D12RootSignature>& root,
                                          D3D12_SHADER_BYTECODE bytecode) {
    D3D12_ROOT_PARAMETER params[2]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants = {1, 2, 2};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    params[1].Descriptor = {2, 1};
    D3D12_ROOT_SIGNATURE_DESC signature{};
    signature.NumParameters = 2;
    signature.pParameters = params;
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize course root signature");
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create course root signature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
    shader.pRootSignature = root.Get();
    shader.CS = bytecode;
    ComPtr<ID3D12PipelineState> pipeline;
    check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                       IID_PPV_ARGS(&pipeline)),
          "create course pipeline");
    return pipeline;
}

void dispatch_wait(ID3D12Device* device) {
    constexpr size_t label = 0, payload = 65536, copied = 2 * 65536;
    constexpr UINT64 expected = 0x12345678;
    ComPtr<ID3D12RootSignature> root;
    auto pipeline = compute_pipeline(device, root, {course_dxil, sizeof(course_dxil)});

    for (bool after_dispatch : {false, true}) {
        Memory memory(3 * 65536);
        std::memset(memory.base + copied, 0xcc, sizeof(expected));
        auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, sizeof(UINT), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                             D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        auto proof = buffer(device, D3D12_HEAP_TYPE_READBACK, sizeof(UINT), D3D12_RESOURCE_STATE_COPY_DEST);
        Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
        auto* list = compute.list.Get();
        direct.write64(memory.address(payload), expected);
        direct.write64(memory.address(label), 1);
        if (!after_dispatch) compute.wait64(memory.address(label), 1);
        auto bind_course = [&] {
            const UINT constants[] = {0x5194c0d1u, 1};
            method<SetRoot>(list, slot_compute_root)(list, root.Get());
            list->SetPipelineState(pipeline.Get());
            method<SetConstants>(list, slot_root_constants)(list, 0, 2, constants, 0);
            method<SetUav>(list, slot_root_uav)(list, 1, output->GetGPUVirtualAddress());
        };
        bind_course();
        list->Dispatch(1, 1, 1);
        if (after_dispatch) compute.wait64(memory.address(label), 1);
        method<CopyMemory_>(list, slot_copy_memory)(
            list, memory.address(copied), memory.address(payload), sizeof(expected), 0);
        compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                    D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(proof.Get(), 0, output.Get(), 0, sizeof(UINT));
        compute.submit();

        // Keep the producer unsubmitted throughout the observation window.
        const auto deadline = GetTickCount64() + 250;
        while (compute.fence->GetCompletedValue() < compute.submitted && GetTickCount64() < deadline)
            Sleep(1);
        const auto observed = compute.fence->GetCompletedValue();
        direct.submit();
        direct.finish("dispatch-wait producer");
        compute.finish("dispatch-wait consumer");
        if (observed == UINT64_MAX) throw std::runtime_error("device removed during dispatch-wait");
        expect_value(memory, label, 1, "producer label");
        expect_value(memory, payload, expected, "producer payload");
        D3D12_RANGE range{0, sizeof(UINT)}, empty{};
        void* mapped{};
        check(proof->Map(0, &range, &mapped), "map dispatch proof");
        const UINT row = *static_cast<const UINT*>(mapped);
        proof->Unmap(0, &empty);
        if (row != 1) throw std::runtime_error("course dispatch did not produce the expected first row");
        const bool early = observed >= compute.submitted;
        const UINT64 value = memory.read64(copied);
        char result[144];
        std::snprintf(result, sizeof(result), "wait=%s completed_before_producer=%s cpu=0x%08llx course=0x%x",
                      after_dispatch ? "after-dispatch" : "list-start", early ? "yes" : "no",
                      static_cast<unsigned long long>(value), row);
        std::fprintf(stderr, "%s\n", result);
        if (early || value != expected) throw std::runtime_error(result);
        if (!after_dispatch) continue;

        // Reuse the guest list. Each prefix must reach its marker before the
        // producer releases that wait, and compute bindings must survive both.
        constexpr size_t progress = 8;
        compute.reopen();
        compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_SOURCE,
                                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        bind_course();
        list->Dispatch(1, 1, 1);
        for (UINT stage : {1u, 2u}) {
            compute.write64(memory.address(progress), stage);
            compute.wait64(memory.address(label), stage + 1);
        }
        const UINT seed = 2;
        method<SetConstants>(list, slot_root_constants)(list, 0, 1, &seed, 0);
        list->Dispatch(1, 1, 1);
        compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                    D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(proof.Get(), 0, output.Get(), 0, sizeof(UINT));
        compute.submit();
        bool prefixes_in_order = true;
        for (UINT stage : {1u, 2u}) {
            const auto until = GetTickCount64() + deadline_ms;
            while (memory.read64(progress) < stage && GetTickCount64() < until) Sleep(1);
            prefixes_in_order &= memory.read64(progress) == stage && compute.fence->GetCompletedValue() < compute.submitted;
            direct.reopen();
            direct.write64(memory.address(label), stage + 1);
            direct.submit();
            direct.finish("release dispatch-wait stage");
        }
        compute.finish("resumed dispatch after two waits");
        if (!prefixes_in_order) throw std::runtime_error("compute prefixes did not reach their waits in order");
        check(proof->Map(0, &range, &mapped), "map resumed dispatch proof");
        const UINT resumed = *static_cast<const UINT*>(mapped);
        proof->Unmap(0, &empty);
        if ((resumed & 255) != seed) throw std::runtime_error("compute bindings lost across waits");
    }
}

ComPtr<ID3D12CommandSignature> dispatch_signature(ID3D12Device* device) {
    D3D12_INDIRECT_ARGUMENT_DESC argument{};
    argument.Type = D3D12_INDIRECT_ARGUMENT_TYPE_DISPATCH;
    D3D12_COMMAND_SIGNATURE_DESC indirect{};
    indirect.ByteStride = sizeof(D3D12_DISPATCH_ARGUMENTS);
    indirect.NumArgumentDescs = 1;
    indirect.pArgumentDescs = &argument;
    ComPtr<ID3D12CommandSignature> command;
    check(device->CreateCommandSignature(&indirect, nullptr, IID_PPV_ARGS(&command)), "create command signature");
    return command;
}

// ExecuteIndirect on its Series X|S slot runs the course shader from an
// argument buffer, and matches the same course dispatched directly.
void indirect_dispatch(ID3D12Device* device) {
    ComPtr<ID3D12RootSignature> root;
    auto pipeline = compute_pipeline(device, root, {course_dxil, sizeof(course_dxil)});

    auto command = dispatch_signature(device);
    const D3D12_DISPATCH_ARGUMENTS groups{(course_length + 7) / 8, 1, 1};
    auto arguments = buffer(device, D3D12_HEAP_TYPE_UPLOAD, sizeof(groups), D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(arguments.Get(), &groups, sizeof(groups));

    constexpr UINT64 bytes = sizeof(Course);
    ComPtr<ID3D12Resource> output[2], readback[2];
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    auto* list = compute.list.Get();
    const UINT constants[] = {0x5194c0d1u, course_length};
    method<SetRoot>(list, slot_compute_root)(list, root.Get());
    list->SetPipelineState(pipeline.Get());
    method<SetConstants>(list, slot_root_constants)(list, 0, 2, constants, 0);
    for (int pass = 0; pass < 2; ++pass) {
        output[pass] = buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                              D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        readback[pass] = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
        method<SetUav>(list, slot_root_uav)(list, 1, output[pass]->GetGPUVirtualAddress());
        if (pass == 0)
            list->Dispatch(groups.ThreadGroupCountX, 1, 1);
        else
            method<ExecuteIndirect_>(list, slot_execute_indirect)(list, command.Get(), 1, arguments.Get(), 0,
                                                                  nullptr, 0);
        compute_fixture::transition(list, output[pass].Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                    D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback[pass].Get(), 0, output[pass].Get(), 0, bytes);
    }
    compute.submit();
    compute.finish("direct and indirect course dispatches");

    Course rows[2]{};
    for (int pass = 0; pass < 2; ++pass) {
        D3D12_RANGE range{0, bytes}, empty{};
        void* mapped{};
        check(readback[pass]->Map(0, &range, &mapped), "map course readback");
        std::memcpy(rows[pass].data(), mapped, bytes);
        readback[pass]->Unmap(0, &empty);
    }
    if (rows[0].front() != 0x0001) throw std::runtime_error("direct course dispatch wrote an unexpected course");
    if (rows[0] != rows[1]) throw std::runtime_error("indirect dispatch differs from the direct dispatch");
}

// The producer and ExecuteIndirect share a compute list. Only the final
// readback waits on a CPU fence.
void gpu_indirect_dispatch(ID3D12Device* device) {
    constexpr UINT words = 24, sentinel = 0xdeadbeef;
    constexpr UINT arg_bytes = sizeof(D3D12_DISPATCH_ARGUMENTS);
    ComPtr<ID3D12RootSignature> root;
    auto pipeline = compute_pipeline(device, root, {gpu_indirect_dxil, sizeof(gpu_indirect_dxil)});
    auto command = dispatch_signature(device);
    std::array<UINT, 3 + words> initial;
    initial.fill(sentinel);
    initial[0] = 3; // Stale arguments would write all 24 positions.
    initial[1] = initial[2] = 1;
    auto seed = buffer(device, D3D12_HEAP_TYPE_UPLOAD, sizeof(initial), D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(seed.Get(), initial.data(), sizeof(initial));
    for (UINT groups : {0u, 1u, 2u}) {
        auto args = buffer(device, D3D12_HEAP_TYPE_DEFAULT, arg_bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                           D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, words * sizeof(UINT), D3D12_RESOURCE_STATE_COPY_DEST,
                             D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
        auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, sizeof(initial), D3D12_RESOURCE_STATE_COPY_DEST);
        Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
        auto* list = compute.list.Get();
        list->CopyBufferRegion(args.Get(), 0, seed.Get(), 0, arg_bytes);
        list->CopyBufferRegion(output.Get(), 0, seed.Get(), arg_bytes, words * sizeof(UINT));
        compute_fixture::transition(list, args.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        method<SetRoot>(list, slot_compute_root)(list, root.Get());
        list->SetPipelineState(pipeline.Get());
        const UINT producer[]{1, groups};
        method<SetConstants>(list, slot_root_constants)(list, 0, 2, producer, 0);
        method<SetUav>(list, slot_root_uav)(list, 1, args->GetGPUVirtualAddress());
        list->Dispatch(1, 1, 1);
        compute_fixture::transition(list, args.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT);
        const UINT consumer = 0;
        method<SetConstants>(list, slot_root_constants)(list, 0, 1, &consumer, 0);
        method<SetUav>(list, slot_root_uav)(list, 1, output->GetGPUVirtualAddress());
        method<ExecuteIndirect_>(list, slot_execute_indirect)(list, command.Get(), 1, args.Get(), 0, nullptr, 0);
        compute_fixture::transition(list, args.Get(), D3D12_RESOURCE_STATE_INDIRECT_ARGUMENT, D3D12_RESOURCE_STATE_COPY_SOURCE);
        compute_fixture::transition(list, output.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        list->CopyBufferRegion(readback.Get(), 0, args.Get(), 0, arg_bytes);
        list->CopyBufferRegion(readback.Get(), arg_bytes, output.Get(), 0, words * sizeof(UINT));
        compute.submit();
        compute.finish("GPU-written indirect dispatch");

        decltype(initial) actual{};
        D3D12_RANGE range{0, sizeof(actual)}, empty{};
        void* mapped{};
        check(readback->Map(0, &range, &mapped), "map indirect arguments and output");
        std::memcpy(actual.data(), mapped, sizeof(actual));
        readback->Unmap(0, &empty);
        if (actual[0] != groups || actual[1] != 1 || actual[2] != 1)
            throw std::runtime_error("incorrect GPU-generated dispatch arguments");
        UINT changed = 0;
        for (UINT i = 0; i < words; ++i) {
            const UINT value = actual[3 + i];
            changed += value != sentinel;
            if (value != (i < 8 * groups ? i + 1 : sentinel))
                throw std::runtime_error("GPU indirect output mismatch at group count " +
                                         std::to_string(groups) + ", word " + std::to_string(i));
        }
        std::fprintf(stderr, "gpu-indirect-dispatch groups=%u changed=%u untouched=%u\n", groups, changed, words - changed);
    }
}

// CopyMemoryToMemoryX reads a guest texture back into title memory, as linear
// rows at the copyable footprint's pitch.
void texture_readback(ID3D12Device* device) {
    constexpr UINT width = 256, height = 256, pitch = 256;
    constexpr UINT64 bytes = UINT64(pitch) * height;
    Memory image(4 * 65536);
    Memory result(2 * 65536);
    D3D12_RESOURCE_DESC desc{};
    desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D;
    desc.Width = width;
    desc.Height = height;
    desc.DepthOrArraySize = 1;
    desc.MipLevels = 1;
    desc.SampleDesc.Count = 1;
    desc.Format = DXGI_FORMAT_R8_UNORM;
    desc.Layout = static_cast<D3D12_TEXTURE_LAYOUT>(0x105);
    ComPtr<ID3D12Resource> texture;
    check(method<Place>(device, device_place)(device, image.address(), &desc, D3D12_RESOURCE_STATE_COMMON,
                                              nullptr, IID_PPV_ARGS(&texture)), "place guest texture");
    auto upload = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    std::vector<BYTE> expected(bytes);
    for (size_t i = 0; i < bytes; ++i) expected[i] = static_cast<BYTE>(i * 7 + i / pitch * 3);
    compute_fixture::upload(upload.Get(), expected.data(), bytes);

    D3D12_TEXTURE_COPY_LOCATION target{}, source{};
    target.Type = D3D12_TEXTURE_COPY_TYPE_SUBRESOURCE_INDEX;
    target.pResource = texture.Get();
    source.Type = D3D12_TEXTURE_COPY_TYPE_PLACED_FOOTPRINT;
    source.pResource = upload.Get();
    source.PlacedFootprint.Footprint = {DXGI_FORMAT_R8_UNORM, width, height, 1, pitch};
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    method<frame_abi::Copy>(direct.list.Get(), slot_copy_texture)(direct.list.Get(), &target, 0, 0, 0, &source,
                                                                  nullptr, 0);
    // The write promoted the texture to a copy destination. It must leave that
    // state before it can be read.
    compute_fixture::transition(direct.list.Get(), texture.Get(), D3D12_RESOURCE_STATE_COPY_DEST,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    method<CopyMemory_>(direct.list.Get(), slot_copy_memory)(direct.list.Get(), result.address(),
                                                             image.address(), bytes, 0);
    direct.submit();
    direct.finish("texture readback");
    if (std::memcmp(result.base, expected.data(), bytes))
        throw std::runtime_error("texture readback differs from the uploaded texels");
}

// `count` timed calls, in microseconds, sorted.
template<class Timed>
std::vector<double> timed_us(unsigned count, Timed timed) {
    LARGE_INTEGER frequency{}, before{}, after{};
    QueryPerformanceFrequency(&frequency);
    std::vector<double> readings(count);
    for (auto& reading : readings) {
        QueryPerformanceCounter(&before);
        timed();
        QueryPerformanceCounter(&after);
        reading = double(after.QuadPart - before.QuadPart) * 1e6 / double(frequency.QuadPart);
    }
    std::sort(readings.begin(), readings.end());
    return readings;
}

template<class Timed>
double median_us(unsigned count, Timed timed) {
    return timed_us(count, timed)[count / 2];
}

// The translation of the console shader that preparation left in the cache, or
// nothing when it has not been prepared.
std::vector<char> prepared_translation() {
    wchar_t temporary[MAX_PATH + 1]{};
    if (!GetTempPathW(MAX_PATH, temporary)) return {};
    const auto file = std::filesystem::path(temporary) / L"tuxbox_shader_cache" / L"by_source" /
                      (std::wstring(xbox_shader_digest) + L".cso");
    std::ifstream input(file, std::ios::binary);
    return {std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>()};
}

struct Readings {
    double submit_us, roundtrip_us, copy_us;
    // Zero when the console shader's translation is not prepared.
    double shader_us, dxil_us;
};

Readings measure(ID3D12Device* device) {
    Readings result{};
    Memory memory(4 * 65536);
    {
        // One list holding a fence write: record, submit and wait.
        Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        UINT64 value = 0;
        result.submit_us = median_us(300, [&] {
            if (value) direct.reopen();
            direct.write64(memory.address(0), ++value);
            direct.submit();
            direct.finish("benchmark submission");
        });
    }
    {
        // A label handed from the direct queue to a waiting compute queue.
        Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
        Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        UINT64 value = 0;
        result.roundtrip_us = median_us(200, [&] {
            if (value) {
                compute.reopen();
                direct.reopen();
            }
            ++value;
            compute.wait64(memory.address(8), value);
            compute.write64(memory.address(16), value);
            compute.submit();
            direct.write64(memory.address(8), value);
            direct.submit();
            direct.finish("benchmark label writer");
            compute.finish("benchmark label waiter");
        });
    }
    {
        Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
        bool used = false;
        result.copy_us = median_us(200, [&] {
            if (used) direct.reopen();
            used = true;
            method<CopyMemory_>(direct.list.Get(), slot_copy_memory)(
                direct.list.Get(), memory.address(2 * 65536), memory.address(65536), 65536, 0);
            direct.submit();
            direct.finish("benchmark memory copy");
        });
    }
    const auto translation = prepared_translation();
    if (!translation.empty()) {
        // The same pipeline twice over: from the console shader, which the
        // provider looks up in the translation cache, and from the translation
        // itself. The difference is what the lookup costs.
        D3D12_ROOT_SIGNATURE_DESC signature{};
        ComPtr<ID3DBlob> serialized;
        check(D3D12SerializeRootSignature(&signature, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
              "serialize empty root signature");
        ComPtr<ID3D12RootSignature> root;
        check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                          IID_PPV_ARGS(&root)), "create empty root signature");
        auto create = [&](const void* bytes, size_t size) {
            D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
            shader.pRootSignature = root.Get();
            shader.CS = {bytes, size};
            ComPtr<ID3D12PipelineState> pipeline;
            check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                               IID_PPV_ARGS(&pipeline)),
                  "benchmark pipeline");
        };
        result.shader_us = median_us(100, [&] { create(xbox_shader, sizeof(xbox_shader)); });
        result.dxil_us = median_us(100, [&] { create(translation.data(), translation.size()); });
    }
    return result;
}

// TuxBox's XboxBvhBuilder, loaded by name as a title's builder calls reach it.
struct Builder {
    using Construct = void* (WINAPI*)(void*, ID3D12Device*, bool);
    using Destroy = void (WINAPI*)(void*);
    using Build = void (WINAPI*)(void*, ID3D12GraphicsCommandList*,
                                 const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC*, UINT,
                                 const D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC*);
    // D3D12XBOX_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_BATCHED_DESC2.
    struct Entry {
        D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC desc;
        UINT64 report_count;
        const D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC* reports;
        UINT64 stream_bytes;
        const void* stream;
    };
    using Batch = void (WINAPI*)(void*, ID3D12GraphicsCommandList*, UINT, const Entry*);
    using CopyStructure = void (WINAPI*)(void*, ID3D12GraphicsCommandList*, D3D12_GPU_VIRTUAL_ADDRESS,
                                         D3D12_GPU_VIRTUAL_ADDRESS,
                                         D3D12_RAYTRACING_ACCELERATION_STRUCTURE_COPY_MODE);
    using Prebuild = void (WINAPI*)(void*, const D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS*,
                                    const UINT64*,
                                    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO*);

    HMODULE library;
    alignas(8) BYTE object[24]{};
    explicit Builder(ID3D12Device* device)
        : library(LoadLibraryExW(L"XboxBvhBuilder.dll", nullptr, 0)) {
        if (!library) throw std::runtime_error("load XboxBvhBuilder.dll");
        entry<Construct>(1)(object, device, false);
    }
    ~Builder() {
        entry<Destroy>(2)(object);
        FreeLibrary(library);
    }
    Builder(const Builder&) = delete;
    Builder& operator=(const Builder&) = delete;
    template<class Function> Function entry(WORD ordinal) const {
        auto address = GetProcAddress(library, MAKEINTRESOURCEA(ordinal));
        if (!address) throw std::runtime_error("missing XboxBvhBuilder export");
        return reinterpret_cast<Function>(address);
    }
};

void unordered_barrier(ID3D12GraphicsCommandList* list) {
    D3D12_RESOURCE_BARRIER barrier{};
    barrier.Type = D3D12_RESOURCE_BARRIER_TYPE_UAV;
    list->ResourceBarrier(1, &barrier);
}

// A bottom-level structure built singly with a size report, copied, and built
// again through the batched call, all by way of the console's builder library.
void ray_tracing_builder(ID3D12Device* device) {
    Builder builder(device);
    const float triangle[] = {0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 1.f, 0.f};
    auto vertices = buffer(device, D3D12_HEAP_TYPE_UPLOAD, sizeof(triangle), D3D12_RESOURCE_STATE_GENERIC_READ);
    compute_fixture::upload(vertices.Get(), triangle, sizeof(triangle));
    D3D12_RAYTRACING_GEOMETRY_DESC geometry{};
    geometry.Type = D3D12_RAYTRACING_GEOMETRY_TYPE_TRIANGLES;
    geometry.Flags = D3D12_RAYTRACING_GEOMETRY_FLAG_OPAQUE;
    geometry.Triangles.VertexFormat = DXGI_FORMAT_R32G32B32_FLOAT;
    geometry.Triangles.VertexCount = 3;
    geometry.Triangles.VertexBuffer = {vertices->GetGPUVirtualAddress(), 3 * sizeof(float)};
    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_INPUTS inputs{};
    inputs.Type = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_TYPE_BOTTOM_LEVEL;
    inputs.Flags = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_BUILD_FLAG_ALLOW_COMPACTION;
    inputs.NumDescs = 1;
    inputs.pGeometryDescs = &geometry;
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_PREBUILD_INFO size{};
    builder.entry<Builder::Prebuild>(7)(builder.object, &inputs, nullptr, &size);
    if (!size.ResultDataMaxSizeInBytes || !size.ScratchDataSizeInBytes)
        throw std::runtime_error("no prebuild sizes; ray tracing is unavailable");

    auto gpu_buffer = [&](UINT64 bytes, D3D12_RESOURCE_STATES state) {
        return buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, state,
                      D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    };
    ComPtr<ID3D12Resource> structure[3];
    for (auto& item : structure)
        item = gpu_buffer(size.ResultDataMaxSizeInBytes,
                          D3D12_RESOURCE_STATE_RAYTRACING_ACCELERATION_STRUCTURE);
    auto scratch = gpu_buffer(size.ScratchDataSizeInBytes, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    auto reports = gpu_buffer(32, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, 32, D3D12_RESOURCE_STATE_COPY_DEST);

    D3D12_BUILD_RAYTRACING_ACCELERATION_STRUCTURE_DESC build{};
    build.Inputs = inputs;
    build.DestAccelerationStructureData = structure[0]->GetGPUVirtualAddress();
    build.ScratchAccelerationStructureData = scratch->GetGPUVirtualAddress();
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC report[2]{};
    for (int i = 0; i < 2; ++i) {
        report[i].DestBuffer = reports->GetGPUVirtualAddress() + 8 * i;
        report[i].InfoType = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_COMPACTED_SIZE;
    }
    Memory label(65536);
    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    auto* list = direct.list.Get();
    builder.entry<Builder::Build>(3)(builder.object, list, &build, 1, &report[0]);
    unordered_barrier(list);
    builder.entry<Builder::CopyStructure>(5)(builder.object, list, structure[1]->GetGPUVirtualAddress(),
                                             structure[0]->GetGPUVirtualAddress(),
                                             D3D12_RAYTRACING_ACCELERATION_STRUCTURE_COPY_MODE_CLONE);
    unordered_barrier(list);
    // The size of the built structure and of its copy, which a missing copy
    // leaves empty.
    D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_DESC current{};
    current.DestBuffer = reports->GetGPUVirtualAddress() + 16;
    current.InfoType = D3D12_RAYTRACING_ACCELERATION_STRUCTURE_POSTBUILD_INFO_CURRENT_SIZE;
    const D3D12_GPU_VIRTUAL_ADDRESS measured[] = {structure[0]->GetGPUVirtualAddress(),
                                                  structure[1]->GetGPUVirtualAddress()};
    method<EmitSizes>(list, slot_emit_sizes)(list, &current, 2, measured);
    unordered_barrier(list);
    Builder::Entry entry{};
    entry.desc = build;
    entry.desc.DestAccelerationStructureData = structure[2]->GetGPUVirtualAddress();
    entry.report_count = 1;
    entry.reports = &report[1];
    builder.entry<Builder::Batch>(4)(builder.object, list, 1, &entry);
    unordered_barrier(list);
    compute_fixture::transition(list, reports.Get(), D3D12_RESOURCE_STATE_UNORDERED_ACCESS,
                                D3D12_RESOURCE_STATE_COPY_SOURCE);
    list->CopyBufferRegion(readback.Get(), 0, reports.Get(), 0, 32);
    direct.write64(label.address(), 21);
    direct.submit();
    direct.finish("acceleration structure builds and copy");
    expect_value(label, 0, 21, "write after the builds");

    D3D12_RANGE range{0, 32}, empty{};
    UINT64* mapped{};
    check(readback->Map(0, &range, reinterpret_cast<void**>(&mapped)), "map size reports");
    const UINT64 single = mapped[0], batched = mapped[1], original = mapped[2], copied = mapped[3];
    readback->Unmap(0, &empty);
    if (!single || single > size.ResultDataMaxSizeInBytes)
        throw std::runtime_error("the single build reported no compacted size");
    if (batched != single) throw std::runtime_error("the batched build was not recorded like the single one");
    if (!original || copied != original) throw std::runtime_error("the acceleration structure was not copied");
}

// Slot 80 (dispatch_flagged) with USE_THREAD_DIMENSIONS (0x20) counts threads
// rather than groups, converting via the bound PSO's [numthreads(8, 1, 1)].
void thread_dimensions(ID3D12Device* device) {
    D3D12_ROOT_PARAMETER params[2]{};
    params[0].ParameterType = D3D12_ROOT_PARAMETER_TYPE_32BIT_CONSTANTS;
    params[0].Constants = {1, 2, 2};
    params[1].ParameterType = D3D12_ROOT_PARAMETER_TYPE_UAV;
    params[1].Descriptor = {2, 1};
    D3D12_ROOT_SIGNATURE_DESC sig{};
    sig.NumParameters = 2;
    sig.pParameters = params;
    ComPtr<ID3DBlob> serialized;
    check(D3D12SerializeRootSignature(&sig, D3D_ROOT_SIGNATURE_VERSION_1, &serialized, nullptr),
          "serialize thread-dimensions root signature");
    ComPtr<ID3D12RootSignature> root;
    check(device->CreateRootSignature(0, serialized->GetBufferPointer(), serialized->GetBufferSize(),
                                      IID_PPV_ARGS(&root)), "create thread-dimensions root signature");
    D3D12_COMPUTE_PIPELINE_STATE_DESC shader{};
    shader.pRootSignature = root.Get();
    shader.CS = {course_dxil, sizeof(course_dxil)};
    ComPtr<ID3D12PipelineState> pipeline, stream_pipeline;
    check(method<CreateComputePipeline>(device, device_create_compute)(device, &shader, 0, nullptr,
                                                                       IID_PPV_ARGS(&pipeline)),
          "create thread-dimensions compute pipeline");
    struct alignas(void*) ComputeStream {
        int root_tag = 0;
        UINT pad0 = 0;
        ID3D12RootSignature* root = nullptr;
        int cs_tag = 6;
        UINT pad1 = 0;
        D3D12_SHADER_BYTECODE cs{};
    } stream{};
    static_assert(sizeof(ComputeStream) == 40);
    stream.root = root.Get();
    stream.cs = shader.CS;
    D3D12_PIPELINE_STATE_STREAM_DESC stream_desc{sizeof(stream), &stream};
    check(method<CreateStreamPipeline>(device, device_create_stream)(device, &stream_desc,
                                                                     IID_PPV_ARGS(&stream_pipeline)),
          "create thread-dimensions stream pipeline");

    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    auto dispatch_flagged = method<DispatchFlagged>(compute.list.Get(), slot_dispatch_flagged);

    // Fail closed when flags & 0x20 is set without a bound compute pipeline.
    dispatch_flagged(compute.list.Get(), 8, 1, 1, 0x20);
    if (SUCCEEDED(compute.list->Close()))
        throw std::runtime_error("slot 80 thread-dimension dispatch accepted without a pipeline");
    check(compute.list->Reset(compute.allocator.Get(), pipeline.Get()), "reset after missing pipeline");
    compute.list->ClearState(nullptr);
    dispatch_flagged(compute.list.Get(), 8, 1, 1, 0x8020);
    if (SUCCEEDED(compute.list->Close()))
        throw std::runtime_error("slot 80 thread-dimension dispatch accepted after ClearState(nullptr)");

    constexpr UINT words = 64, guard = 0xdeadbeefu;
    constexpr UINT64 bytes = words * sizeof(UINT);
    std::array<UINT, words> seed_words;
    seed_words.fill(guard);
    auto seed = buffer(device, D3D12_HEAP_TYPE_UPLOAD, bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto output = buffer(device, D3D12_HEAP_TYPE_DEFAULT, bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                         D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto readback = buffer(device, D3D12_HEAP_TYPE_READBACK, bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    compute_fixture::upload(seed.Get(), seed_words.data(), bytes);

    struct Pass {
        UINT x;
        UINT flags;
        UINT row_count;
        UINT expected_written;
        UINT mode; // 0 = Reset(pipeline), 1 = SetPipelineState(stream_pipeline), 2 = ClearState(pipeline)
    };
    constexpr Pass passes[] = {
        {16, 0x20, words, 16, 0},
        {8, 0x8020, words, 8, 1},
        {70000, 0x20, 24, 24, 2},
        {13, 0x20, 13, 13, 0},
        {0, 0x20, words, 0, 0},
        {2, 0x0, words, 16, 0},
        {1, 0x8000, words, 8, 1},
    };
    const UINT seed_value = 7;
    auto expected_word = [&](UINT row) {
        const UINT beat = (row + seed_value) & 3u;
        const UINT coin = beat == 3u ? 1u : beat;
        UINT mixed = (row + seed_value) * 747796405u + 2891336453u;
        mixed ^= mixed >> 16;
        const UINT barrier = (coin + 1u + (mixed & 1u)) % 3u;
        return coin | (barrier << 8);
    };
    for (size_t i = 0; i < std::size(passes); ++i) {
        const auto& pass = passes[i];
        check(compute.allocator->Reset(), "reset thread-dimensions allocator");
        check(compute.list->Reset(compute.allocator.Get(), pass.mode == 0 ? pipeline.Get() : nullptr),
              "reset thread-dimensions list");
        if (i != 0)
            compute_fixture::transition(compute.list.Get(), output.Get(),
                                        D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_COPY_DEST);
        compute.list->CopyBufferRegion(output.Get(), 0, seed.Get(), 0, bytes);
        compute_fixture::transition(compute.list.Get(), output.Get(),
                                    D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        if (pass.mode == 1)
            compute.list->SetPipelineState(stream_pipeline.Get());
        else if (pass.mode == 2)
            compute.list->ClearState(pipeline.Get());
        auto* raw = compute.list.Get();
        const UINT constants[] = {seed_value, pass.row_count};
        method<SetRoot>(raw, slot_compute_root)(raw, root.Get());
        method<SetConstants>(raw, slot_root_constants)(raw, 0, 2, constants, 0);
        method<SetUav>(raw, slot_root_uav)(raw, 1, output->GetGPUVirtualAddress());
        dispatch_flagged(raw, pass.x, 1, 1, pass.flags);
        compute_fixture::transition(compute.list.Get(), output.Get(),
                                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        compute.list->CopyBufferRegion(readback.Get(), 0, output.Get(), 0, bytes);
        compute.submit();
        compute.finish("slot 80 dispatch");

        std::array<UINT, words> actual{};
        D3D12_RANGE range{0, static_cast<SIZE_T>(bytes)}, empty{};
        void* mapped{};
        check(readback->Map(0, &range, &mapped), "map thread-dimensions readback");
        std::memcpy(actual.data(), mapped, bytes);
        readback->Unmap(0, &empty);
        for (UINT row = 0; row < words; ++row) {
            const UINT expected = row < pass.expected_written ? expected_word(row) : guard;
            if (actual[row] != expected) {
                char msg[160];
                std::snprintf(msg, sizeof(msg),
                              "pass %zu (x=%u flags=%#x) row %u: expected %#x, found %#x",
                              i, pass.x, pass.flags, row, expected, actual[row]);
                throw std::runtime_error(msg);
            }
        }
    }
}

// Exercise shader-visible descriptor slot reclamation on an oversized paged heap:
// (1) sustained >2x turnover with mixed table lengths, (2) overlapping in-flight
// Reset() survival, (3) ClearState pin retention, (4) re-binding an evicted slot
// with fresh view data, and (5) fail-closed E_OUTOFMEMORY without corruption.
void descriptor_reclaim(ID3D12Device* device) {
    constexpr UINT block_16k = 16384;
    constexpr UINT block_8k = 8192;
    constexpr UINT num_16k_blocks = 128;
    constexpr UINT heap_descriptors = num_16k_blocks * block_16k; // 2,097,152 > 1,000,000 native limit
    constexpr UINT max_dispatches = 1024;
    constexpr UINT sentinel = 0xdeadbeefu;

    D3D12_DESCRIPTOR_HEAP_DESC heap_desc{
        D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV, heap_descriptors, D3D12_DESCRIPTOR_HEAP_FLAG_SHADER_VISIBLE, 1};
    ComPtr<ID3D12DescriptorHeap> view_heap;
    // The steps below count on a resident limit of 1,000,000 descriptors. The
    // provider takes the host's MaxViewDescriptorHeapSize when that is larger,
    // and then step 5 never overflows. Pin the limit while this heap is created.
    if (!SetEnvironmentVariableW(L"TUXBOX_DESCRIPTOR_HEAP_LIMIT", L"1000000"))
        throw std::runtime_error("cannot pin the descriptor heap limit");
    const HRESULT created = device->CreateDescriptorHeap(&heap_desc, IID_PPV_ARGS(&view_heap));
    SetEnvironmentVariableW(L"TUXBOX_DESCRIPTOR_HEAP_LIMIT", nullptr);
    check(created, "create oversized view heap");
    const UINT stride = device->GetDescriptorHandleIncrementSize(D3D12_DESCRIPTOR_HEAP_TYPE_CBV_SRV_UAV);
    if (!stride) throw std::runtime_error("zero view descriptor stride");
    const SIZE_T cpu_base = method<frame_abi::CpuHandle>(view_heap.Get(), 9)(view_heap.Get());
    const UINT64 gpu_base = method<GpuHandle>(view_heap.Get(), 10)(view_heap.Get());
    auto cpu_at = [&](UINT index) -> D3D12_CPU_DESCRIPTOR_HANDLE {
        return {cpu_base + SIZE_T(index) * stride};
    };
    auto gpu_at = [&](UINT index) -> D3D12_GPU_DESCRIPTOR_HANDLE {
        return {gpu_base + UINT64(index) * stride};
    };

    auto make_pipeline = [&](UINT table_len, ComPtr<ID3D12RootSignature>& rs, ComPtr<ID3D12PipelineState>& pso) {
        D3D12_DESCRIPTOR_RANGE ranges[3]{};
        ranges[0] = {D3D12_DESCRIPTOR_RANGE_TYPE_CBV, 1, 0, 0, 0};
        ranges[1] = {D3D12_DESCRIPTOR_RANGE_TYPE_SRV, 1, 0, 0, 1};
        ranges[2] = {D3D12_DESCRIPTOR_RANGE_TYPE_UAV, 1, 0, 0, table_len - 1};
        D3D12_ROOT_PARAMETER param{};
        param.ParameterType = D3D12_ROOT_PARAMETER_TYPE_DESCRIPTOR_TABLE;
        param.DescriptorTable = {3, ranges};
        D3D12_ROOT_SIGNATURE_DESC sig{1, &param, 0, nullptr, D3D12_ROOT_SIGNATURE_FLAG_NONE};
        ComPtr<ID3DBlob> blob;
        check(D3D12SerializeRootSignature(&sig, D3D_ROOT_SIGNATURE_VERSION_1, &blob, nullptr),
              "serialize reclaim root signature");
        check(device->CreateRootSignature(0, blob->GetBufferPointer(), blob->GetBufferSize(), IID_PPV_ARGS(&rs)),
              "create reclaim root signature");
        D3D12_COMPUTE_PIPELINE_STATE_DESC pso_desc{};
        pso_desc.pRootSignature = rs.Get();
        pso_desc.CS = {reclaim_dxil, sizeof(reclaim_dxil)};
        check(method<CreateComputePipeline>(device, device_create_compute)(
                  device, &pso_desc, 0, nullptr, IID_PPV_ARGS(&pso)),
              "create reclaim compute pipeline");
    };
    ComPtr<ID3D12RootSignature> rs16, rs8;
    ComPtr<ID3D12PipelineState> pso16, pso8;
    make_pipeline(block_16k, rs16, pso16);
    make_pipeline(block_8k, rs8, pso8);

    auto cb_key = [](UINT k) { return 0x1000u + k * 7u + 3u; };
    auto cb_salt = [](UINT k) { return 0x50000u + k * 13u + 5u; };
    auto src_word = [](UINT k) { return 0x200u + k * 19u + 11u; };
    auto expected_word = [&](UINT k) { return src_word(k) * 131u + cb_key(k) * 17u + cb_salt(k); };

    std::vector<UINT> cb_bytes(size_t(max_dispatches) * 64, 0);
    std::vector<UINT> src_words(max_dispatches, 0);
    std::vector<UINT> seed_words(max_dispatches, sentinel);
    std::vector<UINT> expected_out(max_dispatches, sentinel);
    for (UINT k = 0; k < max_dispatches; ++k) {
        cb_bytes[size_t(k) * 64 + 0] = cb_key(k);
        cb_bytes[size_t(k) * 64 + 1] = cb_salt(k);
        src_words[k] = src_word(k);
    }
    const UINT64 out_bytes = UINT64(max_dispatches) * sizeof(UINT);
    auto cb_pool = buffer(device, D3D12_HEAP_TYPE_UPLOAD, UINT64(max_dispatches) * 256,
                          D3D12_RESOURCE_STATE_GENERIC_READ);
    auto src_pool = buffer(device, D3D12_HEAP_TYPE_UPLOAD, out_bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto seed_buf = buffer(device, D3D12_HEAP_TYPE_UPLOAD, out_bytes, D3D12_RESOURCE_STATE_GENERIC_READ);
    auto dst_buf = buffer(device, D3D12_HEAP_TYPE_DEFAULT, out_bytes, D3D12_RESOURCE_STATE_COPY_DEST,
                          D3D12_RESOURCE_FLAG_ALLOW_UNORDERED_ACCESS);
    auto readback_buf = buffer(device, D3D12_HEAP_TYPE_READBACK, out_bytes, D3D12_RESOURCE_STATE_COPY_DEST);
    compute_fixture::upload(cb_pool.Get(), cb_bytes.data(), cb_bytes.size() * sizeof(UINT));
    compute_fixture::upload(src_pool.Get(), src_words.data(), size_t(out_bytes));
    compute_fixture::upload(seed_buf.Get(), seed_words.data(), size_t(out_bytes));

    const UINT64 cb_va = cb_pool->GetGPUVirtualAddress();
    auto author_table = [&](UINT slot_base, UINT table_len, UINT data_idx, UINT out_idx) {
        D3D12_CONSTANT_BUFFER_VIEW_DESC cbv{cb_va + UINT64(data_idx) * 256, 256};
        device->CreateConstantBufferView(&cbv, cpu_at(slot_base + 0));
        D3D12_SHADER_RESOURCE_VIEW_DESC srv{};
        srv.ViewDimension = D3D12_SRV_DIMENSION_BUFFER;
        srv.Shader4ComponentMapping = D3D12_DEFAULT_SHADER_4_COMPONENT_MAPPING;
        srv.Buffer.FirstElement = data_idx;
        srv.Buffer.NumElements = 1;
        srv.Buffer.StructureByteStride = sizeof(UINT);
        device->CreateShaderResourceView(src_pool.Get(), &srv, cpu_at(slot_base + 1));
        D3D12_UNORDERED_ACCESS_VIEW_DESC uav{};
        uav.ViewDimension = D3D12_UAV_DIMENSION_BUFFER;
        uav.Buffer.FirstElement = out_idx;
        uav.Buffer.NumElements = 1;
        uav.Buffer.StructureByteStride = sizeof(UINT);
        device->CreateUnorderedAccessView(dst_buf.Get(), nullptr, &uav, cpu_at(slot_base + table_len - 1));
        expected_out[out_idx] = expected_word(data_idx);
    };
    auto bind_and_dispatch = [&](ID3D12GraphicsCommandList* list, UINT slot_base, UINT table_len, bool set_pso = true) {
        ID3D12DescriptorHeap* heaps[]{view_heap.Get()};
        method<BindHeaps>(list, slot_bind_heaps)(list, 1, heaps);
        ID3D12RootSignature* rs = table_len == block_16k ? rs16.Get() : rs8.Get();
        ID3D12PipelineState* pso = table_len == block_16k ? pso16.Get() : pso8.Get();
        method<SetRoot>(list, slot_compute_root)(list, rs);
        if (set_pso) list->SetPipelineState(pso);
        method<SetTable>(list, slot_compute_table)(list, 0, gpu_at(slot_base));
        list->Dispatch(1, 1, 1);
    };

    Queue direct(device, D3D12_COMMAND_LIST_TYPE_DIRECT);
    Queue compute(device, D3D12_COMMAND_LIST_TYPE_COMPUTE);
    direct.list->CopyBufferRegion(dst_buf.Get(), 0, seed_buf.Get(), 0, out_bytes);
    compute_fixture::transition(direct.list.Get(), dst_buf.Get(),
                                D3D12_RESOURCE_STATE_COPY_DEST, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
    direct.submit();
    direct.finish("seed reclaim output buffer");

    auto verify_outputs = [&](UINT check_count, const char* phase) {
        direct.reopen();
        compute_fixture::transition(direct.list.Get(), dst_buf.Get(),
                                    D3D12_RESOURCE_STATE_UNORDERED_ACCESS, D3D12_RESOURCE_STATE_COPY_SOURCE);
        direct.list->CopyBufferRegion(readback_buf.Get(), 0, dst_buf.Get(), 0, out_bytes);
        compute_fixture::transition(direct.list.Get(), dst_buf.Get(),
                                    D3D12_RESOURCE_STATE_COPY_SOURCE, D3D12_RESOURCE_STATE_UNORDERED_ACCESS);
        direct.submit();
        direct.finish(phase);
        D3D12_RANGE range{0, SIZE_T(out_bytes)}, empty{};
        const UINT* mapped = nullptr;
        check(readback_buf->Map(0, &range, reinterpret_cast<void**>(const_cast<UINT**>(&mapped))),
              "map reclaim readback");
        for (UINT i = 0; i < check_count; ++i) {
            if (mapped[i] != expected_out[i]) {
                const UINT got = mapped[i];
                readback_buf->Unmap(0, &empty);
                char msg[192];
                std::snprintf(msg, sizeof(msg), "%s mismatch at output %u: expected 0x%08x, found 0x%08x",
                              phase, i, expected_out[i], got);
                throw std::runtime_error(msg);
            }
        }
        readback_buf->Unmap(0, &empty);
    };

    // 1. Sustained turnover (> 2x shader-visible limit) mixing 8K and 16K spans.
    //    Emit 32 8K tables (blocks 0..16) interleaved with 16K blocks 16..67,
    //    then 61 16K blocks 67..128 so all of 0..67 are evicted by the end.
    struct TableJob { UINT slot_base; UINT len; UINT idx; };
    std::vector<TableJob> jobs;
    jobs.reserve(144);
    UINT next_idx = 0;
    for (UINT i = 0; i < 32; ++i) {
        jobs.push_back({i * block_8k, block_8k, next_idx++});
        jobs.push_back({(16 + i) * block_16k, block_16k, next_idx++});
    }
    for (UINT b = 48; b < 128; ++b) {
        jobs.push_back({b * block_16k, block_16k, next_idx++});
    }
    for (const auto& job : jobs) author_table(job.slot_base, job.len, job.idx, job.idx);
    auto release_leases = [](Queue& q) {
        q.reopen();
        q.close();
    };
    for (size_t offset = 0; offset < jobs.size(); offset += 8) {
        if (offset > 0) compute.reopen();
        const size_t batch_end = std::min(offset + 8, jobs.size());
        for (size_t i = offset; i < batch_end; ++i)
            bind_and_dispatch(compute.list.Get(), jobs[i].slot_base, jobs[i].len);
        compute.submit();
        compute.finish("sustained descriptor turnover batch");
    }
    release_leases(compute);
    verify_outputs(next_idx, "sustained turnover");

    // 2. Overlapping / double-buffered in-flight safety across CPU Reset():
    //    L1 (20 tables) is queued behind gate_fence, L0 (30 tables) runs & finishes
    //    on direct (so L1's last_used is OLDER than L0's), L2 (15 tables) queues
    //    behind gate_fence, L1->Reset() drops L1's recording leases while L1 is
    //    still in-flight, and L3 (25 tables) forces eviction of L0 without touching L1/L2.
    const UINT sub2_base = next_idx;
    for (UINT b = 0; b < 90; ++b) {
        author_table(b * block_16k, block_16k, next_idx, next_idx);
        ++next_idx;
    }
    auto make_list = [&](D3D12_COMMAND_LIST_TYPE type, ComPtr<ID3D12CommandAllocator>& alloc,
                         ComPtr<ID3D12GraphicsCommandList>& list) {
        check(device->CreateCommandAllocator(type, IID_PPV_ARGS(&alloc)), "create extra allocator");
        const UINT list_desc[] = {static_cast<UINT>(type), 0, 1, 0};
        check(method<frame_abi::CreateList>(device, device_create_list)(
                  device, list_desc, alloc.Get(), nullptr, IID_PPV_ARGS(&list)),
              "create extra list");
    };
    {
        ComPtr<ID3D12Fence> gate_fence;
        check(device->CreateFence(0, D3D12_FENCE_FLAG_NONE, IID_PPV_ARGS(&gate_fence)), "create gate fence");
        check(method<WaitQueue>(compute.queue.Get(), queue_wait)(compute.queue.Get(), gate_fence.Get(), 1),
              "stall compute queue on gate fence");

        ComPtr<ID3D12CommandAllocator> alloc_l1, alloc_l2, alloc_l3;
        ComPtr<ID3D12GraphicsCommandList> list_l1, list_l2;
        make_list(D3D12_COMMAND_LIST_TYPE_COMPUTE, alloc_l1, list_l1);
        make_list(D3D12_COMMAND_LIST_TYPE_COMPUTE, alloc_l2, list_l2);
        check(device->CreateCommandAllocator(D3D12_COMMAND_LIST_TYPE_COMPUTE, IID_PPV_ARGS(&alloc_l3)),
              "create spare L3 allocator");

        check(list_l1->Reset(alloc_l1.Get(), nullptr), "open L1");
        for (UINT b = 0; b < 20; ++b) bind_and_dispatch(list_l1.Get(), b * block_16k, block_16k);
        check(list_l1->Close(), "close L1");
        {
            ID3D12CommandList* batch[]{list_l1.Get()};
            compute.queue->ExecuteCommandLists(1, batch);
        }

        direct.reopen();
        for (UINT b = 20; b < 50; ++b) bind_and_dispatch(direct.list.Get(), b * block_16k, block_16k);
        direct.submit();
        direct.finish("subtest 2 L0");
        release_leases(direct);

        check(list_l2->Reset(alloc_l2.Get(), nullptr), "open L2");
        for (UINT b = 50; b < 65; ++b) bind_and_dispatch(list_l2.Get(), b * block_16k, block_16k);
        check(list_l2->Close(), "close L2");
        {
            ID3D12CommandList* batch[]{list_l2.Get()};
            compute.queue->ExecuteCommandLists(1, batch);
        }

        // Reset L1 on the CPU while L1 is still in-flight behind gate_fence!
        check(list_l1->Reset(alloc_l3.Get(), nullptr), "reset L1 while in-flight");
        for (UINT b = 65; b < 90; ++b) bind_and_dispatch(list_l1.Get(), b * block_16k, block_16k);
        check(list_l1->Close(), "close L3");
        {
            ID3D12CommandList* batch[]{list_l1.Get()};
            compute.queue->ExecuteCommandLists(1, batch);
        }
        check(gate_fence->Signal(1), "release gate fence");
        compute.signal();
        compute.finish("overlapping L1/L2/L3 execution");
    }
    verify_outputs(sub2_base + 90, "overlapping in-flight reset");

    // 3. ClearState pin retention:
    //    Fill 0..61 on direct, open CL and bind+dispatch table A (block 61),
    //    call ClearState, refresh 1..61 on direct (making A oldest), then bind
    //    60 new tables 62..122 (ending with table B at block 121) inside CL.
    for (UINT b = 0; b < 122; ++b) {
        author_table(b * block_16k, block_16k, next_idx, next_idx);
        ++next_idx;
    }
    direct.reopen();
    for (UINT b = 0; b < 61; ++b) bind_and_dispatch(direct.list.Get(), b * block_16k, block_16k);
    direct.submit();
    direct.finish("subtest 3 fill");
    release_leases(direct);

    compute.reopen();
    bind_and_dispatch(compute.list.Get(), 61u * block_16k, block_16k); // table A (block 61)
    compute.list->ClearState(pso16.Get());

    direct.reopen();
    for (UINT b = 1; b < 61; ++b) bind_and_dispatch(direct.list.Get(), b * block_16k, block_16k);
    direct.submit();
    direct.finish("subtest 3 refresh 1..61");
    release_leases(direct);

    // Bind 60 new tables 62..122 after ClearState; keep pso16 from ClearState(pso16).
    for (UINT b = 62; b < 122; ++b)
        bind_and_dispatch(compute.list.Get(), b * block_16k, block_16k, false);
    compute.submit();
    compute.finish("subtest 3 ClearState CL");
    verify_outputs(next_idx, "ClearState pin retention");

    // 4. Re-bind after eviction with fresh view data at logical block 0 (evicted in step 3).
    const UINT fresh_out = next_idx++;
    author_table(0, block_16k, fresh_out, fresh_out);
    compute.reopen();
    bind_and_dispatch(compute.list.Get(), 0, block_16k);
    compute.submit();
    compute.finish("re-bind evicted slot 0");
    release_leases(compute);
    verify_outputs(next_idx, "re-bind evicted slot");

    // 5. Fail-closed OOM control:
    //    Pin block 0 in open list L_pinned, then pin 62 more unique tables (total 63 * 16K
    //    = 1,032,192 > 1,000,000 limit) in open list L_oom and confirm Close() returns
    //    E_OUTOFMEMORY without corrupting L_pinned or preventing post-Reset recovery.
    const UINT pinned_out = next_idx++;
    const UINT recovery_out = next_idx++;
    author_table(0, block_16k, pinned_out, pinned_out);
    author_table(1 * block_16k, block_16k, recovery_out, recovery_out);

    ComPtr<ID3D12CommandAllocator> alloc_pin, alloc_oom;
    ComPtr<ID3D12GraphicsCommandList> list_pin, list_oom;
    make_list(D3D12_COMMAND_LIST_TYPE_COMPUTE, alloc_pin, list_pin);
    make_list(D3D12_COMMAND_LIST_TYPE_COMPUTE, alloc_oom, list_oom);

    check(list_pin->Reset(alloc_pin.Get(), nullptr), "open pinned control list");
    bind_and_dispatch(list_pin.Get(), 0, block_16k);

    check(list_oom->Reset(alloc_oom.Get(), nullptr), "open OOM control list");
    for (UINT b = 1; b <= 62; ++b) bind_and_dispatch(list_oom.Get(), b * block_16k, block_16k);
    const HRESULT oom_hr = list_oom->Close();
    if (oom_hr != E_OUTOFMEMORY) {
        char msg[128];
        std::snprintf(msg, sizeof(msg), "expected E_OUTOFMEMORY on unsubmitted pin overflow, got 0x%08lx",
                      static_cast<unsigned long>(oom_hr));
        throw std::runtime_error(msg);
    }

    check(list_pin->Close(), "close pinned control list after OOM");
    {
        ID3D12CommandList* batch[]{list_pin.Get()};
        compute.queue->ExecuteCommandLists(1, batch);
    }
    check(list_oom->Reset(alloc_oom.Get(), nullptr), "reset failed OOM list");
    bind_and_dispatch(list_oom.Get(), 1 * block_16k, block_16k);
    check(list_oom->Close(), "close recovery list");
    {
        ID3D12CommandList* batch[]{list_oom.Get()};
        compute.queue->ExecuteCommandLists(1, batch);
    }
    compute.signal();
    compute.finish("pinned control and post-OOM recovery");
    verify_outputs(next_idx, "fail-closed OOM control");
}

struct Scenario {
    const wchar_t* name;
    void (*run)(ID3D12Device*);
};
constexpr Scenario scenarios[] = {
    {L"labels", labels},
    {L"cross-queue", cross_queue},
    {L"compute-frame", compute_frame},
    {L"skipped-command", skipped_command},
    {L"copy-memory", copy_memory},
    {L"direct-copy-wait", direct_copy_wait},
    {L"shared-texture", shared_texture},
    {L"indirect-dispatch", indirect_dispatch},
    {L"gpu-indirect-dispatch", gpu_indirect_dispatch},
    {L"dispatch-wait", dispatch_wait},
    {L"texture-readback", texture_readback},
    {L"ray-tracing-builder", ray_tracing_builder},
    {L"thread-dimensions", thread_dimensions},
    {L"descriptor-reclaim", descriptor_reclaim},
    {L"xbox-shader", xbox_shader_scenario},
    {L"wave64-shader", wave64_shader_scenario},
    {L"raster-tables", raster_tables},
    {L"wave-alloc", wave_alloc_scenario},
    {L"raster-samplers", raster_samplers},
    {L"compute-root", compute_root},
    {L"mesh-samplers", mesh_samplers},
    {L"mesh-sampler-single", mesh_sampler_single},
    {L"compute-root-recovery", compute_root_recovery},
};

// A fresh Scarlett device on the requested adapter. The factory is reached
// through the Xbox device, so the game imports no desktop graphics library.
ComPtr<ID3D12Device> create_scarlett_device(const wchar_t* requested, char (&pci)[16]) {
    auto create = [](IUnknown* adapter, ComPtr<ID3D12Device>& out) {
        return D3D12XboxCreateDevice(adapter, scarlett_profile.data(), __uuidof(ID3D12Device11),
                                     reinterpret_cast<void**>(out.ReleaseAndGetAddressOf()));
    };
    auto matches = [&](IDXGIAdapter1* adapter) {
        DXGI_ADAPTER_DESC1 desc{};
        check(adapter->GetDesc1(&desc), "adapter description");
        std::snprintf(pci, sizeof(pci), "%04x:%04x", desc.VendorId, desc.DeviceId);
        wchar_t found[16];
        std::swprintf(found, 16, L"%04x:%04x", desc.VendorId, desc.DeviceId);
        return !(desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) && (!requested || !_wcsicmp(requested, found));
    };
    ComPtr<ID3D12Device> device;
    check(create(nullptr, device), "Scarlett device");
    ComPtr<IDXGIDevice1> dxgi;
    check(device.As(&dxgi), "Xbox device DXGI interface");
    ComPtr<IDXGIAdapter> adapter;
    check(dxgi->GetAdapter(&adapter), "Xbox device adapter");
    ComPtr<IDXGIAdapter1> adapter1;
    check(adapter.As(&adapter1), "Xbox device adapter interface");
    if (matches(adapter1.Get())) return device;
    ComPtr<IDXGIFactory1> factory;
    check(adapter->GetParent(IID_PPV_ARGS(&factory)), "Xbox adapter parent");
    for (UINT n = 0;; ++n) {
        ComPtr<IDXGIAdapter1> candidate;
        const HRESULT hr = factory->EnumAdapters1(n, &candidate);
        if (hr == DXGI_ERROR_NOT_FOUND) throw std::runtime_error("no matching Xbox D3D12 hardware adapter");
        check(hr, "adapter enumeration");
        if (!matches(candidate.Get())) continue;
        check(create(candidate.Get(), device), "Scarlett device on selected adapter");
        return device;
    }
}

std::string narrow(const wchar_t* text) {
    std::string result;
    while (*text) result += static_cast<char>(*text++);
    return result;
}
}

const wchar_t* const selftest_scenarios[] = {L"labels",            L"cross-queue",      L"compute-frame",
                                             L"skipped-command",   L"copy-memory",      L"direct-copy-wait", L"shared-texture",
                                             L"indirect-dispatch", L"gpu-indirect-dispatch",
                                             L"dispatch-wait", L"texture-readback", L"ray-tracing-builder",
                                             L"thread-dimensions", L"descriptor-reclaim",
                                             L"xbox-shader", L"wave64-shader",
                                             L"raster-tables", L"wave-alloc", L"raster-samplers", L"compute-root",
                                             L"mesh-samplers", L"mesh-sampler-single",
                                             L"compute-root-recovery"};
const unsigned selftest_scenario_count = static_cast<unsigned>(std::size(selftest_scenarios));
static_assert(std::size(selftest_scenarios) == std::size(scenarios));

std::string run_selftest(const wchar_t* requested, const wchar_t* adapter) {
    const bool all = !std::wcscmp(requested, L"all");
    unsigned passed = 0;
    char pci[16]{};
    for (const auto& scenario : scenarios) {
        if (!all && std::wcscmp(requested, scenario.name)) continue;
        // This one writes nothing until the launcher's compiler has translated it,
        // and mesh-samplers needs its root-aware runtime shader worker.
        if (all && (!std::wcscmp(scenario.name, L"wave64-shader") || !std::wcsncmp(scenario.name, L"mesh-sampler", 12)))
            continue;
        const auto name = narrow(scenario.name);
        try {
            auto device = create_scarlett_device(adapter, pci);
            scenario.run(device.Get());
        } catch (const std::exception& error) {
            throw std::runtime_error(name + ": " + error.what());
        }
        ++passed;
        // Progress, so a crash or a stuck later scenario can be located.
        std::fprintf(stderr, "lanes-selftest %s ok\n", name.c_str());
    }
    if (!passed) throw std::runtime_error("unknown scenario " + narrow(requested));
    return "lanes-selftest PASS pci=" + std::string(pci) + " scenario=" + narrow(requested) +
           " passed=" + std::to_string(passed);
}

std::string run_benchmark(const wchar_t* adapter) {
    char pci[16]{};
    Readings readings{};
    {
        auto device = create_scarlett_device(adapter, pci);
        readings = measure(device.Get());
    }
    // The game's own frame: paint the board, upload it through the Xbox command
    // list and read it back, without a window.
    std::string selected;
    auto device = create_lanes_device(adapter, selected);
    auto game = std::make_unique<Game>();
    generate_course(device.Get(), game->rows, 0x5194c0d1u);
    Screen screen(device.Get());
    const auto frames = timed_us(300, [&] {
        game->move(0);
        if (game->finish != Finish::playing) game->restart();
        screen.verify(*game);
    });
    char line[320];
    int used = std::snprintf(line, sizeof(line),
                             "lanes-benchmark PASS pci=%s submit_us=%.1f roundtrip_us=%.1f copy64k_us=%.1f "
                             "frame_us=%.1f frame_p99_us=%.1f",
                             pci, readings.submit_us, readings.roundtrip_us, readings.copy_us,
                             frames[frames.size() / 2], frames[frames.size() * 99 / 100]);
    if (readings.shader_us > 0)
        std::snprintf(line + used, sizeof(line) - used, " shader_pipeline_us=%.1f dxil_pipeline_us=%.1f",
                      readings.shader_us, readings.dxil_us);
    return line;
}
