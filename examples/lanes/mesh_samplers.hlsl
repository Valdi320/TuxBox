// The desktop translation of the `mesh-samplers` console mesh program, which
// `raster_fixture.py` builds from records only. `tuxbox.py verify` compiles it
// into the prepared cache under that program's digest: a fullscreen triangle
// whose only output is the position the console pixel program reads.
struct Vertex {
    float4 position : SV_Position;
};

[numthreads(1, 1, 1)]
[outputtopology("triangle")]
void main(out vertices Vertex corners[3], out indices uint3 triangles[1]) {
    SetMeshOutputCounts(3, 1);
    static const float2 at[3] = {float2(-1, -1), float2(-1, 3), float2(3, -1)};
    for (uint i = 0; i < 3; ++i)
        corners[i].position = float4(at[i], 0.5, 1);
    triangles[0] = uint3(0, 1, 2);
}
