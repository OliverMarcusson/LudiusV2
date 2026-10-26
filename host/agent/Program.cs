using Google.Protobuf;
using Grpc.Core;
using Ludius.HostAgent;
using Ludius.V1;
using System.Net.WebSockets;
using System.Text;
using System.Threading.Channels;
using Microsoft.Extensions.FileProviders;

var builder = WebApplication.CreateBuilder(args);
builder.Services.AddGrpc();
builder.Services.AddSingleton(new HyperV(Guid.Parse(builder.Configuration["Agent:ObserverService"]!)));
builder.Services.AddSingleton<FeedHub>();
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

        // OpenRouter request counts the controller writes (counts and timestamps only). Shared read so the
        // controller can still replace the file while it is open here.
        var calls = Path.GetFullPath(app.Configuration["Monitor:Calls"] ?? "../../runs/openrouter-calls.json", app.Environment.ContentRootPath);
        api.MapGet("/calls", () =>
        {
            try
            {
                using var f = new FileStream(calls, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                return Results.Text(new StreamReader(f).ReadToEnd(), "application/json");
            }
            catch (FileNotFoundException) { return Results.Json(new { jev = 0, llm = 0 }); }
            catch (DirectoryNotFoundException) { return Results.Json(new { jev = 0, llm = 0 }); }
        });

        // A 12 fps feed per VM: binary messages are JPEG frames, text messages report why there is no frame.
        app.UseWebSockets();
        api.MapGet("/vms/{name}/stream", async (HttpContext ctx, string name, int? w, FeedHub hub) =>
        {
            if (!include.IsMatch(name) || !HyperV.ValidName(name)) return Results.NotFound();
            if (!ctx.WebSockets.IsWebSocketRequest) return Results.BadRequest("WebSocket only");
            // WebSockets ignore CORS: without this any page in an operator's browser could watch the desktops.
            var origin = ctx.Request.Headers.Origin.ToString();
            if (origin.Length > 0 && !Uri.TryCreate(origin, UriKind.Absolute, out var o) | (o?.Authority != ctx.Request.Host.Value))
                return Results.StatusCode(StatusCodes.Status403Forbidden);
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            var ct = ctx.RequestAborted;
            using var feed = hub.Subscribe(name, Math.Clamp(w ?? 640, 160, 1920));
            var closed = ws.ReceiveAsync(new byte[16], ct); // view only: any client message ends the feed
            try
            {
                while (true)
                {
                    var next = feed.Reader.ReadAsync(ct).AsTask();
                    if (await Task.WhenAny(next, closed) == closed) break;
                    var frame = await next;
                    if (frame.Jpeg is { } jpeg) await ws.SendAsync(jpeg, WebSocketMessageType.Binary, true, ct);
                    else await ws.SendAsync(Encoding.UTF8.GetBytes(frame.Error!), WebSocketMessageType.Text, true, ct);
                }
                if (ws.State == WebSocketState.Open) await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, null, ct);
            }
            catch (Exception e) when (e is OperationCanceledException or WebSocketException) { }
            return Results.Empty;
        });
    }
}

// One capture loop per VM and size, shared by every viewer and stopped with the last one. A tick every
// 1/12 s starts a capture unless three are already running; frames go out in order, older ones are dropped.
sealed class FeedHub(HyperV hv)
{
    const int Fps = 12;
    const int MaxInFlight = 3;

    public readonly record struct Frame(byte[]? Jpeg, string? Error);

    sealed class Feed
    {
        public readonly List<Channel<Frame>> Viewers = [];
        public readonly CancellationTokenSource Stop = new();
        public long Sent;
        public DateTime QuietUntil; // after an error (VM off): one attempt per second, not twelve
    }

    public sealed class Subscription(ChannelReader<Frame> reader, Action leave) : IDisposable
    {
        public ChannelReader<Frame> Reader => reader;
        public void Dispose() => leave();
    }

    readonly Dictionary<(string, int), Feed> feeds = [];

    public Subscription Subscribe(string vm, int width)
    {
        var key = (vm, width);
        var ch = Channel.CreateBounded<Frame>(new BoundedChannelOptions(2) { FullMode = BoundedChannelFullMode.DropOldest, SingleReader = true });
        lock (feeds)
        {
            if (!feeds.TryGetValue(key, out var feed))
            {
                feeds[key] = feed = new Feed();
                _ = Run(vm, width, feed);
            }
            feed.Viewers.Add(ch);
            return new Subscription(ch.Reader, () =>
            {
                lock (feeds)
                {
                    feed.Viewers.Remove(ch);
                    if (feed.Viewers.Count > 0) return;
                    feed.Stop.Cancel();
                    feeds.Remove(key);
                }
            });
        }
    }

    async Task Run(string vm, int width, Feed feed)
    {
        using var slots = new SemaphoreSlim(MaxInFlight);
        using var timer = new PeriodicTimer(TimeSpan.FromSeconds(1.0 / Fps));
        long seq = 0;
        try
        {
            while (await timer.WaitForNextTickAsync(feed.Stop.Token))
            {
                if (DateTime.UtcNow < feed.QuietUntil || !slots.Wait(0)) continue; // host slower than 12 fps: skip a tick
                var n = ++seq;
                _ = Task.Run(() =>
                {
                    try { Publish(feed, n, new Frame(hv.Frame(vm, width), null)); }
                    catch (Exception e)
                    {
                        feed.QuietUntil = DateTime.UtcNow.AddSeconds(1);
                        Publish(feed, n, new Frame(null, e is InvalidOperationException or KeyNotFoundException ? e.Message : "capture failed"));
                    }
                    finally { slots.Release(); }
                });
            }
        }
        catch (OperationCanceledException) { }
    }

    void Publish(Feed feed, long n, Frame frame)
    {
        lock (feeds)
        {
            if (n <= feed.Sent) return; // a slower, older capture finished after a newer one
            feed.Sent = n;
            foreach (var v in feed.Viewers) v.Writer.TryWrite(frame);
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
