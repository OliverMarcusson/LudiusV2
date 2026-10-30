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
var backend = builder.Configuration["Agent:Backend"] ?? (OperatingSystem.IsWindows() ? "hyperv" : "proxmox");
var maxRunning = int.Parse(builder.Configuration["Agent:MaxRunningVms"] ?? "1");
if (maxRunning < 1) throw new InvalidOperationException("Agent:MaxRunningVms must be at least 1");
IHypervisor hypervisor = backend switch
{
#if WINDOWS
    "hyperv" => new HyperV(Guid.Parse(builder.Configuration["Agent:ObserverService"]!),
        Path.GetFullPath(builder.Configuration["Agent:OneVmScript"] ?? "../../infra/hyperv/one-vm.ps1", builder.Environment.ContentRootPath), maxRunning),
#endif
    "proxmox" => new Proxmox(new Pvesh(), builder.Configuration["Proxmox:Node"] ?? Environment.MachineName, ProxmoxConfig(builder.Configuration) with { MaxRunning = maxRunning }),
    _ => throw new InvalidOperationException($"Agent:Backend {backend} is not available in this build"),
};
builder.Services.AddSingleton(hypervisor);
builder.Services.AddSingleton<FeedHub>();
var app = builder.Build();
app.MapGrpcService<AgentService>();
Monitor.Map(app);
if (backend == "hyperv") ClockSweep.Start(app);
app.Run();

static ProxmoxOptions ProxmoxConfig(IConfiguration c)
{
    var o = new ProxmoxOptions();
    if (c["Agent:ObserverService"] is { } svc) o = o with { VsockPort = BitConverter.ToUInt32(Guid.Parse(svc).ToByteArray(), 0) };
    if (c["Proxmox:VsockPort"] is { } port) o = o with { VsockPort = uint.Parse(port) };
    if (c["Proxmox:RunDir"] is { } run) o = o with { RunDir = run };
    if (c["Proxmox:Pool"] is { } pool) o = o with { Pool = pool };
    if (c["Proxmox:ScratchDir"] is { } scratch) o = o with { ScratchDir = scratch };
    if (c["Proxmox:CapExempt"] is { } ex) o = o with { CapExempt = ex.Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .Select(e => e.Split(':', 2) is [var id, var name] && name.StartsWith("ludius-") ? (int.Parse(id), name)
            : throw new InvalidOperationException($"Proxmox:CapExempt entry {e} is not <vmid>:ludius-<name>")).ToHashSet() };
    if (c["Proxmox:Resolution"] is { } res && res.Split('x') is [var w, var h]) o = o with { Screen = (int.Parse(w), int.Parse(h)) };
    return o;
}

static class ClockSweep
{
    public static void Start(WebApplication app)
    {
        var root = app.Environment.ContentRootPath;
        var script = Path.GetFullPath(app.Configuration["Agent:ClockSweepScript"] ?? "../../infra/hyperv/clock-sweep.ps1", root);
        var runs = Environment.GetEnvironmentVariable("LUDIUS_RUNS") is { Length: > 0 } r ? r : Path.GetFullPath("../../runs", root);
        var log = app.Logger;
        var stop = app.Lifetime.ApplicationStopping;
        _ = Task.Run(async () =>
        {
            using var timer = new PeriodicTimer(TimeSpan.FromSeconds(60));
            while (await timer.WaitForNextTickAsync(stop))
            {
                try
                {
                    var psi = new System.Diagnostics.ProcessStartInfo("pwsh",
                        ["-NoProfile", "-NonInteractive", "-File", script, "-Locks", Path.Combine(runs, "locks")])
                        { RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
                    using var p = System.Diagnostics.Process.Start(psi)!;
                    var output = (await p.StandardOutput.ReadToEndAsync(stop) + await p.StandardError.ReadToEndAsync(stop)).Trim();
                    await p.WaitForExitAsync(stop);
                    if (output != "") log.LogWarning("clock sweep: {Output}", output);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception e) { log.LogError(e, "clock sweep failed"); }
            }
        }, stop);
    }
}

static class Monitor
{
    public static void Map(WebApplication app)
    {
        var host = new Uri(app.Configuration["Kestrel:Endpoints:Monitor:Url"]!);
        var onMonitor = $"*:{host.Port}";
        var allowedOrigins = ParseOrigins(app.Configuration["Monitor:AllowedOrigins"]);
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

        var include = new System.Text.RegularExpressions.Regex(app.Configuration["Monitor:Include"] ?? "^ludius-",
            System.Text.RegularExpressions.RegexOptions.IgnoreCase);
        var api = app.MapGroup("/api").RequireHost(onMonitor);
        api.MapGet("/vms", (IHypervisor hv) => hv.ListVms().Where(v => include.IsMatch(v.Name)));

        var calls = Path.GetFullPath(app.Configuration["Monitor:Calls"] ?? "../../runs/openrouter-calls.json", app.Environment.ContentRootPath);
        ConsoleData.Map(api, app, Path.GetDirectoryName(calls)!);
        api.MapGet("/calls", () =>
        {
            try
            {
                using var f = new FileStream(calls, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
                var text = new StreamReader(f).ReadToEnd();
                var o = System.Text.Json.Nodes.JsonNode.Parse(text) as System.Text.Json.Nodes.JsonObject;
                if (o is not null && !o.ContainsKey("decision") && o.Remove("jev", out var old))
                    o["decision"] = old;
                return Results.Text(o?.ToJsonString() ?? text, "application/json");
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException)
            { return Results.Problem("Call counters unavailable", statusCode: 503); }
        });

        var interactions = Path.GetFullPath(app.Configuration["Monitor:Interactions"]
            ?? Path.Combine(Path.GetDirectoryName(calls)!, "monitor-interactions.jsonl"), app.Environment.ContentRootPath);
        api.MapGet("/interactions", () => Results.Json(new { interactions = ConsoleData.LastLines(interactions, 20) }));

        var admission = Path.GetFullPath(app.Configuration["Monitor:Admission"] ?? "../../runs/admission", app.Environment.ContentRootPath);
        api.MapGet("/admission", () => Results.Json(Admission(admission, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())));

        app.UseWebSockets();
        api.MapGet("/vms/{name}/stream", async (HttpContext ctx, string name, int? w, FeedHub hub) =>
        {
            if (!include.IsMatch(name) || !VmName.Valid(name)) return Results.NotFound();
            if (!ctx.WebSockets.IsWebSocketRequest) return Results.BadRequest("WebSocket only");
            if (!OriginAllowed(ctx.Request.Headers.Origin.ToString(), ctx.Request.Host.Value, allowedOrigins))
                return Results.StatusCode(StatusCodes.Status403Forbidden);
            using var ws = await ctx.WebSockets.AcceptWebSocketAsync();
            var ct = ctx.RequestAborted;
            using var feed = hub.Subscribe(name, Math.Clamp(w ?? 640, 160, 1920));
            var closed = ws.ReceiveAsync(new byte[16], ct);
            using var slow = CancellationTokenSource.CreateLinkedTokenSource(ct);
            try
            {
                while (true)
                {
                    var next = feed.Reader.ReadAsync(ct).AsTask();
                    if (await Task.WhenAny(next, closed) == closed) break;
                    var frame = await next;
                    slow.CancelAfter(SlowViewer);
                    if (frame.Jpeg is { } jpeg) await ws.SendAsync(jpeg, WebSocketMessageType.Binary, true, slow.Token);
                    else await ws.SendAsync(Encoding.UTF8.GetBytes(frame.Error!), WebSocketMessageType.Text, true, slow.Token);
                    slow.CancelAfter(Timeout.Infinite);
                }
                if (ws.State == WebSocketState.Open) await ws.CloseAsync(WebSocketCloseStatus.NormalClosure, null, ct);
            }
            catch (Exception e) when (e is OperationCanceledException or WebSocketException) { }
            return Results.Empty;
        });
    }

    static readonly TimeSpan SlowViewer = TimeSpan.FromSeconds(10);

    public static bool OriginAllowed(string origin, string? host, IReadOnlySet<string> allowed) =>
        origin.Length == 0
        || (Uri.TryCreate(origin, UriKind.Absolute, out var o) && o.Authority == host)
        || allowed.Contains(origin);

    public static IReadOnlySet<string> ParseOrigins(string? value)
    {
        var set = new HashSet<string>(StringComparer.Ordinal);
        foreach (var entry in (value ?? "").Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
        {
            if (!Uri.TryCreate(entry, UriKind.Absolute, out var u) || u.Scheme is not ("https" or "http") || u.UserInfo != ""
                || u.Query != "" || u.Fragment != "" || u.AbsolutePath != "/" || entry.EndsWith('/'))
                throw new InvalidOperationException($"Monitor:AllowedOrigins: {entry} is not an origin like https://monitor.example");
            set.Add(u.GetLeftPart(UriPartial.Authority));
        }
        return set;
    }

    static readonly Regex AdmissionLog = new(@"^\d{8}-\d{6}-([a-z0-9_]{1,64})\.jsonl$");
    static readonly Regex Token = new(@"^[A-Za-z0-9_.:+-]{1,64}$");
    const long MaxAdmissionRead = 8 << 20;
    const long ActiveMs = 90_000;

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
            catch (JsonException) { continue; }
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
                    case "admission_run":
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

sealed class FeedHub(IHypervisor hv)
{
    const int Fps = 15;
    const int MaxInFlight = 3;

    public readonly record struct Frame(byte[]? Jpeg, string? Error);

    sealed class Feed
    {
        public readonly List<Channel<Frame>> Viewers = [];
        public readonly CancellationTokenSource Stop = new();
        public long Sent;
        public byte[]? Last;
        public DateTime LastAt;
        public DateTime QuietUntil;
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
        var ch = Channel.CreateBounded<Frame>(new BoundedChannelOptions(1) { FullMode = BoundedChannelFullMode.DropOldest, SingleReader = true });
        lock (feeds)
        {
            if (!feeds.TryGetValue(key, out var feed))
            {
                feeds[key] = feed = new Feed();
                _ = Run(vm, width, feed);
            }
            feed.Viewers.Add(ch);
            if (feed.Last is { } last) ch.Writer.TryWrite(new Frame(last, null));
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
                if (DateTime.UtcNow < feed.QuietUntil || !slots.Wait(0)) continue;
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
            if (n <= feed.Sent) return;
            feed.Sent = n;
            var now = DateTime.UtcNow;
            if (frame.Jpeg is { } jpeg && ReferenceEquals(jpeg, feed.Last) && now - feed.LastAt < TimeSpan.FromSeconds(1)) return;
            (feed.Last, feed.LastAt) = (frame.Jpeg, now);
            foreach (var v in feed.Viewers) v.Writer.TryWrite(frame);
        }
    }
}

sealed class AgentService(IHypervisor hv, ILogger<AgentService> log) : HostAgent.HostAgentBase
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
        if (res.Snapshot.ScreenWidth > 0 && res.Snapshot.ScreenHeight > 0) vm.Screen = (res.Snapshot.ScreenWidth, res.Snapshot.ScreenHeight);
        return new ObserveResult { Snapshot = res.Snapshot, Hello = vm.Channel.Hello, HostTimeMs = Now };
    });

    public override async Task<FileContent> ReadGuestFile(GuestFileRequest r, ServerCallContext ctx) => await Guard(async () =>
    {
        var vm = hv.Get(r.Vm);
        var res = await vm.Channel.Request(new ObserverRequest { ReadFile = r.File ?? throw new ArgumentException("no file") },
            TimeSpan.FromSeconds(30), ctx.CancellationToken);
        return res.BodyCase switch
        {
            ObserverResponse.BodyOneofCase.File => res.File,
            ObserverResponse.BodyOneofCase.Error when res.Error == "unknown request" =>
                throw new RpcException(new Status(StatusCode.Unimplemented, "observer has no read_file")),
            ObserverResponse.BodyOneofCase.Error => throw new InvalidOperationException("observer: " + res.Error),
            _ => throw new InvalidOperationException("observer: no file in the answer"),
        };
    });

    public override Task<AgentInfo> Info(InfoRequest r, ServerCallContext ctx) =>
        Task.FromResult(new AgentInfo { Backend = hv.Backend, Host = Environment.MachineName });

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
        catch (Exception e) when (e is UnauthorizedAccessException) { throw new RpcException(new Status(StatusCode.PermissionDenied, e.Message)); }
        catch (Exception e) when (e is IOException) { throw new RpcException(new Status(StatusCode.Unavailable, e.Message)); }
        catch (Exception e)
        {
            log.LogWarning(e, "agent call failed");
            throw new RpcException(new Status(StatusCode.Internal, e.Message));
        }
    }
}
