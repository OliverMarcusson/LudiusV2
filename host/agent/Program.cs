using Google.Protobuf;
using Grpc.Core;
using Ludius.HostAgent;
using Ludius.V1;
using Microsoft.Extensions.FileProviders;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGrpc();
builder.Services.AddSingleton(new HyperV(Guid.Parse(builder.Configuration["Agent:ObserverService"]!)));
builder.Services.AddSingleton<FrameCache>();
var app = builder.Build();
app.MapGrpcService<AgentService>();
Monitor.Map(app);
app.Run();

// Operator monitor: a view-only web page of the VMs' desktops on its own HTTP/1.1 endpoint.
// GET routes only, and nothing here reaches input, the guest channel or lifecycle.
static class Monitor
{
    public static void Map(WebApplication app)
    {
        var host = new Uri(app.Configuration["Kestrel:Endpoints:Monitor:Url"]!);
        var onMonitor = $"*:{host.Port}";
        app.Use(async (ctx, next) =>
        {
            if (ctx.Connection.LocalPort == host.Port)
            {
                var h = ctx.Response.Headers;
                h.ContentSecurityPolicy = "default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'";
                h.XContentTypeOptions = "nosniff";
                h["Referrer-Policy"] = "no-referrer";
            }
            await next();
        });

        var root = Path.GetFullPath(app.Configuration["Monitor:Root"] ?? "web", app.Environment.ContentRootPath);
        if (Directory.Exists(root))
        {
            var files = new PhysicalFileProvider(root);
            app.UseDefaultFiles(new DefaultFilesOptions { FileProvider = files });
            app.UseStaticFiles(new StaticFileOptions { FileProvider = files });
        }

        // Only range VMs are shown; other VMs on the host stay invisible, also to direct frame requests.
        var include = new System.Text.RegularExpressions.Regex(app.Configuration["Monitor:Include"] ?? "^ludius-",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        var api = app.MapGroup("/api").RequireHost(onMonitor);
        api.MapGet("/vms", (HyperV hv) => hv.ListVms().Where(v => include.IsMatch(v.Name)));
        api.MapGet("/vms/{name}/frame", (string name, int? w, HyperV hv, FrameCache cache, HttpResponse res) =>
        {
            if (!include.IsMatch(name)) return Results.NotFound();
            try
            {
                var jpeg = cache.Get(name, Math.Clamp(w ?? 640, 160, 3840), hv);
                res.Headers.CacheControl = "no-store";
                return Results.File(jpeg, "image/jpeg");
            }
            catch (ArgumentException e) { return Results.BadRequest(e.Message); }
            catch (KeyNotFoundException e) { return Results.NotFound(e.Message); }
            catch (InvalidOperationException e) { return Results.Conflict(e.Message); }
        });
    }
}

// Frames are shared between viewers: at most one thumbnail per VM and size every 400 ms.
sealed class FrameCache
{
    readonly Dictionary<(string, int), (DateTime At, byte[] Jpeg)> frames = [];
    readonly Dictionary<(string, int), object> locks = [];

    public byte[] Get(string vm, int width, HyperV hv)
    {
        var key = (vm, width);
        object gate;
        lock (locks) gate = locks.TryGetValue(key, out var g) ? g : locks[key] = new object();
        lock (gate)
        {
            lock (frames)
                if (frames.TryGetValue(key, out var f) && DateTime.UtcNow - f.At < TimeSpan.FromMilliseconds(400)) return f.Jpeg;
            var jpeg = hv.Frame(vm, width);
            lock (frames) frames[key] = (DateTime.UtcNow, jpeg);
            return jpeg;
        }
    }
}

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
