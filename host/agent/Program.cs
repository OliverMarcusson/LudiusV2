using Google.Protobuf;
using Grpc.Core;
using Ludius.HostAgent;
using Ludius.V1;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGrpc();
builder.Services.AddSingleton(new HyperV(Guid.Parse(builder.Configuration["Agent:ObserverService"]!)));
var app = builder.Build();
app.MapGrpcService<AgentService>();
app.Run();

sealed class AgentService(HyperV hv, ILogger<AgentService> log) : HostAgent.HostAgentBase
{
    static long Now => DateTimeOffset.UtcNow.ToUnixTimeMilliseconds();

    public override async Task<InputResult> Input(InputRequest r, ServerCallContext ctx) => await Guard(async () =>
    {
        var vm = hv.Get(r.Vm);
        var ct = ctx.CancellationToken;
        await vm.InputGate.WaitAsync(ct);
        try
        {
            switch (r.ActionCase)
            {
                case InputRequest.ActionOneofCase.Move: await hv.Move(vm, r.Move.X, r.Move.Y, r.Move.DurationMs, ct); break;
                case InputRequest.ActionOneofCase.Click: await hv.Click(vm, (int)r.Click.Button + 1, r.Click.Count, ct); break;
                case InputRequest.ActionOneofCase.Key: hv.Key(vm, r.Key.Vk, r.Key.Down); break;
                case InputRequest.ActionOneofCase.Chord: await hv.Chord(vm, r.Chord.Vk, ct); break;
                case InputRequest.ActionOneofCase.Type: await hv.Type(vm, r.Type.Text, r.Type.MeanMs, r.Type.JitterMs, ct); break;
                case InputRequest.ActionOneofCase.CtrlAltDel: hv.CtrlAltDel(vm); break;
                case InputRequest.ActionOneofCase.Scroll: await hv.Scroll(vm, r.Scroll.Notches, ct); break;
                default: throw new ArgumentException("no action");
            }
        }
        finally { vm.InputGate.Release(); }
        return new InputResult { HostTimeMs = Now };
    });

    public override async Task<ObserveResult> Observe(ObserveRequest r, ServerCallContext ctx) => await Guard(async () =>
    {
        var vm = hv.Get(r.Vm);
        var req = r.Request ?? new SnapshotRequest { WaitSettle = true };
        var timeout = TimeSpan.FromMilliseconds((req.TimeoutMs == 0 ? 5000 : req.TimeoutMs) + 10000);
        var res = await vm.Channel.Request(new ObserverRequest { Snapshot = req }, timeout, ctx.CancellationToken);
        if (res.BodyCase == ObserverResponse.BodyOneofCase.Error) throw new InvalidOperationException("observer: " + res.Error);
        return new ObserveResult { Snapshot = res.Snapshot, Hello = vm.Channel.Hello, HostTimeMs = Now };
    });

    public override async Task<LifecycleResult> Lifecycle(LifecycleRequest r, ServerCallContext ctx) => await Guard(async () =>
        new LifecycleResult { State = await hv.Lifecycle(r.Vm, r.Op, r.Checkpoint, ctx.CancellationToken) });

    public override async Task<ScreenshotResult> Screenshot(ScreenshotRequest r, ServerCallContext ctx) => await Guard(() =>
    {
        var (png, w, h) = hv.Screenshot(hv.Get(r.Vm));
        return Task.FromResult(new ScreenshotResult { Png = ByteString.CopyFrom(png), Width = w, Height = h });
    });

    async Task<T> Guard<T>(Func<Task<T>> f)
    {
        try { return await f(); }
        catch (RpcException) { throw; }
        catch (OperationCanceledException) { throw new RpcException(new Status(StatusCode.Cancelled, "cancelled")); }
        catch (Exception e) when (e is ArgumentException) { throw new RpcException(new Status(StatusCode.InvalidArgument, e.Message)); }
        catch (Exception e) when (e is KeyNotFoundException) { throw new RpcException(new Status(StatusCode.NotFound, e.Message)); }
        catch (Exception e) when (e is IOException) { throw new RpcException(new Status(StatusCode.Unavailable, e.Message)); }
        catch (Exception e)
        {
            log.LogWarning(e, "agent call failed");
            throw new RpcException(new Status(StatusCode.Internal, e.Message));
        }
    }
}
