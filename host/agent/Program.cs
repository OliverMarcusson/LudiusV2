using Google.Protobuf;
using Grpc.Core;
using Ludius.HostAgent;
using Ludius.V1;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
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

        var admission = Path.GetFullPath(app.Configuration["Monitor:Admission"] ?? "../../runs/admission", app.Environment.ContentRootPath);
        api.MapGet("/admission", () => Results.Json(Admission(admission, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())));

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

    static readonly Regex AdmissionLog = new(@"^\d{8}-\d{6}-([a-z0-9_]{1,64})\.jsonl$");
    static readonly Regex Token = new(@"^[A-Za-z0-9_.:+-]{1,64}$");
    const long MaxAdmissionRead = 8 << 20; // admission logs run to about 200 KB
    const long ActiveMs = 90_000; // the longest quiet stretch inside a batch so far is 44 s

    // Progress of the newest admission log (runs/admission/<started>-<task>.jsonl), as a summary of identifiers,
    // counts and times only: no paths, option texts, typed text or raw lines leave the agent.
    // running: an event within ActiveMs. completed: the log ends with admission_end (stopped: ended early). stale: quiet mid-run, so the
    // worker may be stuck or gone. idle: quiet after a run ended, so the batch finished or stopped (the log can't tell).
    static object Admission(string dir, long now)
    {
        var file = Directory.Exists(dir)
            ? new DirectoryInfo(dir).EnumerateFiles("*.jsonl").Where(f => AdmissionLog.IsMatch(f.Name)).MaxBy(f => f.Name)
            : null;
        if (file is null) return new { state = "none" };

        string? Tok(JsonElement e, string name) =>
            e.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.String or JsonValueKind.Number
            && Token.IsMatch(v.ToString()) ? v.ToString() : null;

        long? last = null;
        string? variant = null, subgoal = null, action = null, result = null, status = null;
        int run = 0, step = 0, done = 0, failed = 0, runDone = 0, runFailed = 0;
        bool inRun = false, ended = false, perRun = false, stopped = false;
        bool? admitted = null;
        var runs = new Dictionary<string, int>();
        using var fs = new FileStream(file.FullName, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        var truncated = fs.Length > MaxAdmissionRead;
        if (truncated) fs.Seek(-MaxAdmissionRead, SeekOrigin.End);
        using var reader = new StreamReader(fs);
        while (reader.ReadLine() is { } line)
        {
            JsonDocument doc;
            try { doc = JsonDocument.Parse(line); }
            catch (JsonException) { continue; } // a line still being written, or the cut at a truncation
            using (doc)
            {
                var e = doc.RootElement;
                if (e.ValueKind != JsonValueKind.Object) continue;
                if (e.TryGetProperty("host_ms", out var ms) && ms.TryGetInt64(out var t)) last = t;
                switch (Tok(e, "event"))
                {
                    case "task_start":
                        variant = Tok(e, "variant");
                        run = runs[variant ?? ""] = runs.GetValueOrDefault(variant ?? "") + 1;
                        (inRun, step, subgoal, action, result, status) = (true, 0, null, null, null, null);
                        break;
                    case "step":
                        subgoal = Tok(e, "subgoal");
                        action = Tok(e, "action");
                        result = Tok(e, "result");
                        step = int.TryParse(Tok(e, "step"), out var s) ? s : step;
                        break;
                    case "task_end":
                        status = Tok(e, "status");
                        if (status == "done") done++; else failed++;
                        inRun = false;
                        break;
                    case "admission_run": // per attempt, also when a run errored before task_end
                        perRun = true;
                        if (Tok(e, "status") == "done") runDone++; else runFailed++;
                        inRun = false;
                        break;
                    case "admission_end":
                        (ended, inRun) = (true, false);
                        stopped = e.TryGetProperty("stopped", out var st) && st.ValueKind == JsonValueKind.String;
                        admitted = e.TryGetProperty("admitted", out var a) && a.ValueKind is JsonValueKind.True or JsonValueKind.False ? a.GetBoolean() : null;
                        break;
                }
            }
        }
        if (perRun) (done, failed) = (runDone, runFailed);
        var age = now - (last ?? new DateTimeOffset(file.LastWriteTimeUtc).ToUnixTimeMilliseconds());
        var state = ended ? "completed" : age < ActiveMs ? "running" : inRun ? "stale" : "idle";
        return new
        {
            state, task = AdmissionLog.Match(file.Name).Groups[1].Value, variant, run, inRun, step, subgoal, action,
            result, status, done, failed, admitted, stopped, ageMs = Math.Max(0, age), truncated,
        };
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
        new LifecycleResult { State = await hv.Lifecycle(r.Vm, r.Op, r.Checkpoint, r.Target, ctx.CancellationToken) });

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
