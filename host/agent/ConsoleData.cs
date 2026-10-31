using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using YamlDotNet.RepresentationModel;

namespace Ludius.HostAgent;

static class ConsoleData
{
    const int MaxRead = 16 << 20;
    static readonly Regex User = new("^[a-z][a-z0-9_-]{0,63}$", RegexOptions.CultureInvariant);
    static readonly Regex DayFile = new(@"^(\d{4}-\d{2}-\d{2})-([a-z][a-z0-9_-]{0,63})$", RegexOptions.CultureInvariant);

    public static void Map(RouteGroupBuilder api, WebApplication app, string defaultRuns)
    {
        var runs = Path.GetFullPath(app.Configuration["Monitor:Runs"] ?? defaultRuns, app.Environment.ContentRootPath);
        var personas = Path.GetFullPath(app.Configuration["Monitor:Personas"] ?? "../../controller/data/personas", app.Environment.ContentRootPath);
        var templates = Path.GetFullPath(app.Configuration["Monitor:Templates"] ?? "../../controller/data/templates", app.Environment.ContentRootPath);
        Portraits.Map(api, app, runs, personas);
        api.MapGet("/templates", () => Read(() => Templates(templates)));
        api.MapGet("/console", () => Read(() => Catalog(runs, personas)));
        api.MapGet("/run", () => Read(() => Run(runs, DateTimeOffset.UtcNow.ToUnixTimeMilliseconds())));
        api.MapGet("/daybook/{date}/{user}", (string date, string user) =>
        {
            if (!ValidDate(date) || !User.IsMatch(user)) return Results.BadRequest();
            return Read(() => Day(runs, date, user));
        });
    }

    static IResult Read(Func<object> read)
    {
        try { return Results.Json(read()); }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or YamlDotNet.Core.YamlException or JsonException or InvalidDataException)
        { return Results.Problem("Console source is unreadable or malformed", statusCode: 503); }
    }

    public static bool ValidDate(string value) => DateOnly.TryParseExact(value, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out _);

    static readonly string[] PersonFields = ["user", "name", "role", "department", "manager"];
    static readonly string[] TraitFields = ["user", "name", "role", "department", "hours", "style", "risk_profile", "tz", "typing_mean_ms", "typing_jitter_ms", "think_mean_s", "pointer_ms"];
    static readonly string[] ItemFields = ["at", "template", "context", "minutes", "interrupt", "handoff"];
    static readonly string[] EventFields = ["event", "host_ms", "logical", "at", "template", "task", "status", "reason", "subgoal", "steps", "by", "clock", "done", "items"];

    static readonly string[] TemplateFields = ["task", "app", "capability", "about", "params", "budget_minutes"];

    static JsonObject[] Templates(string dir) =>
        Directory.GetFiles(dir, "*.yaml").Order().Select(p => Pick(Yaml(p) ?? throw new InvalidDataException(), TemplateFields)).ToArray();

    public static object Catalog(string runs, string personaDir)
    {
        var narrative = Yaml(Path.Combine(runs, "world", "narrative.yaml"));
        var company = narrative is null ? null : Pick(narrative, "company", "city", "summary", "projects", "revised");
        if (company is not null)
        {
            company["people"] = Project(narrative!, "people", PersonFields);
            company["events"] = Project(narrative!, "events", ["date", "what"]);
            company["history"] = Project(narrative!, "history", ["date", "what"]);
        }
        var people = Directory.Exists(personaDir)
            ? Directory.GetFiles(personaDir, "*.yaml").Order().Select(p => Pick(Yaml(p) ?? throw new InvalidDataException(), TraitFields)).ToArray()
            : null;
        if (people is not null && people.Any(p => !User.IsMatch(Text(p, "user")))) throw new InvalidDataException();
        var index = new List<DayIndex>();
        foreach (var folder in new[] { "plans", "days" })
        {
            var dir = Path.Combine(runs, folder);
            if (!Directory.Exists(dir)) continue;
            foreach (var file in Directory.GetFiles(dir, folder == "plans" ? "*.yaml" : "*.jsonl"))
            {
                var m = DayFile.Match(Path.GetFileNameWithoutExtension(file));
                if (m.Success && ValidDate(m.Groups[1].Value)) index.Add(new(m.Groups[1].Value, m.Groups[2].Value, folder));
            }
        }
        return new
        {
            company, people, index,
            latest = people?.Select(p =>
            {
                var user = Text(p, "user");
                // Most recently written, not the highest date: a warp can play a day before an earlier warp's.
                var written = (DayIndex i) => File.GetLastWriteTimeUtc(Path.Combine(runs, i.Source, $"{i.Date}-{user}" + (i.Source == "plans" ? ".yaml" : ".jsonl")));
                var date = index.Where(i => i.User == user && i.Source == "days").OrderBy(written).Select(i => i.Date).LastOrDefault()
                    ?? index.Where(i => i.User == user).OrderBy(written).Select(i => i.Date).LastOrDefault();
                var day = date is null ? null : Day(runs, date, user);
                var events = day?.Events;
                var start = events is null ? -1 : Array.FindLastIndex(events, e => Text(e, "event") == "day_start");
                var ends = events?.Skip(Math.Max(0, start)).Where(e => Text(e, "event") == "item_end").ToArray();
                var next = day?.Plan?.OfType<JsonObject>().FirstOrDefault(item => !(ends?.Any(e => Text(e, "at") == Text(item, "at") && Text(e, "template") == Text(item, "template")) ?? false));
                var since = events?.Skip(Math.Max(0, start)).ToArray();
                var react = since is null ? -1 : Array.FindLastIndex(since, e => Text(e, "event") == "reaction_start");
                var reacting = react >= 0 && !since![react..].Any(e => Text(e, "event") == "reaction_end")
                    ? new { at = Text(since[react], "logical").Split('T').Last(), template = Text(since[react], "template"), context = Text(since[react], "context") }
                    : null;
                return new { user, date, done = ends?.Count(e => Text(e, "status") == "done"), abandoned = ends?.Count(e => Text(e, "status") == "abandoned"), next, reacting };
            }).ToArray(),
            sources = new { narrative = company is null ? "missing" : "ok", people = people is null ? "missing" : "ok",
                plans = Directory.Exists(Path.Combine(runs, "plans")) ? "ok" : "missing", days = Directory.Exists(Path.Combine(runs, "days")) ? "ok" : "missing" },
        };
    }

    public record DayIndex(string Date, string User, string Source);
    public record DayRecord(string Date, string User, JsonArray? Plan, JsonObject[]? Events, object Sources);

    public static DayRecord Day(string runs, string date, string user)
    {
        if (!ValidDate(date) || !User.IsMatch(user)) throw new InvalidDataException();
        var stem = $"{date}-{user}";
        var plan = Yaml(Path.Combine(runs, "plans", stem + ".yaml"));
        if (plan is not null && (Text(plan, "persona") != user || Text(plan, "date") != date)) throw new InvalidDataException();
        var events = Lines(Path.Combine(runs, "days", stem + ".jsonl"));
        return new(date, user, plan is null ? null : Project(plan, "items", ItemFields),
            events?.Select(e => Pick(e, EventFields)).ToArray(),
            new { plan = plan is null ? "missing" : "ok", recording = events is null ? "missing" : "ok" });
    }

    static JsonArray Project(JsonObject root, string key, string[] fields)
    {
        if (root[key] is null) return [];
        if (root[key] is not JsonArray list) throw new InvalidDataException();
        return new JsonArray(list.Select(n => n is JsonObject o ? Pick(o, fields) : throw new InvalidDataException()).Cast<JsonNode?>().ToArray());
    }

    static JsonObject Pick(JsonObject root, params string[] fields)
    {
        var result = new JsonObject();
        foreach (var k in fields) if (root[k] is { } v) result[k] = v.DeepClone();
        return result;
    }

    static string Text(JsonObject o, string k) => o[k] is JsonValue v && v.TryGetValue<string>(out var s) ? s : "";

    static string? FileText(string path)
    {
        try
        {
            using var f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            if (f.Length > MaxRead) throw new InvalidDataException();
            using var reader = new StreamReader(f);
            var text = new System.Text.StringBuilder();
            var chars = new char[8192];
            int length;
            while ((length = reader.Read(chars, 0, chars.Length)) > 0)
            {
                if (text.Length + length > MaxRead) throw new InvalidDataException();
                text.Append(chars, 0, length);
            }
            return text.ToString();
        }
        catch (FileNotFoundException) { return null; }
        catch (DirectoryNotFoundException) { return null; }
    }

    static JsonObject? Yaml(string path)
    {
        var text = FileText(path);
        if (text is null) return null;
        var stream = new YamlStream();
        stream.Load(new StringReader(text));
        if (stream.Documents.Count != 1 || Convert(stream.Documents[0].RootNode) is not JsonObject result) throw new InvalidDataException();
        return result;
    }

    static JsonNode? Convert(YamlNode node) => node switch
    {
        YamlScalarNode s => JsonValue.Create(s.Value ?? ""),
        YamlSequenceNode s => new JsonArray(s.Children.Select(Convert).ToArray()),
        YamlMappingNode m => new JsonObject(m.Children.Select(p => new KeyValuePair<string, JsonNode?>(((YamlScalarNode)p.Key).Value ?? "", Convert(p.Value)))),
        _ => throw new InvalidDataException(),
    };

    internal static object Run(string runs, long now)
    {
        var journal = Lines(Path.Combine(runs, "range", "warp.jsonl"));
        if (journal is null || journal.Count == 0) return new { phase = "none" };
        string phase = "none", date = "", error = "";
        string? logical = null;
        long setAt = 0, last = 0;
        var (day, planned) = (0, false);
        List<string> users = [], unplanned = [];
        var results = new Dictionary<string, (string Status, long At)>();
        static long HostMs(JsonObject e) => DateTime.TryParse(Text(e, "at"), CultureInfo.InvariantCulture, DateTimeStyles.AssumeLocal, out var t)
            ? new DateTimeOffset(t).ToUnixTimeMilliseconds() : 0;
        foreach (var e in journal)
        {
            last = HostMs(e);
            switch (Text(e, "event"))
            {
                case "clock_set":
                    if (phase is "finished" or "stopped" or "failed") (phase, day, planned, error) = ("none", 0, false, "");
                    (logical, setAt) = (Text(e, "logical"), last);
                    if (planned) (phase, planned) = ("preparing", false);
                    else { (phase, date) = ("planning", ""); day++; users = []; unplanned = []; results.Clear(); }
                    break;
                case "no_plan" when User.IsMatch(Text(e, "persona")): unplanned.Add(Text(e, "persona")); break;
                case "planned":
                    date = ValidDate(Text(e, "date")) ? Text(e, "date") : "";
                    users = (e["items"] as JsonObject)?.Where(p => User.IsMatch(p.Key) && p.Value is JsonValue v && v.TryGetValue<int>(out var n) && n > 0)
                        .Select(p => p.Key).ToList() ?? [];
                    planned = users.Count > 0;
                    break;
                case "day" when User.IsMatch(Text(e, "persona")): results[Text(e, "persona")] = (e["result"] is JsonArray ? "done" : "failed", last); break;
                case "interrupted": phase = "stopped"; break;
                case "warp_failed" or "clock_reset_failed": phase = "failed"; error = Text(e, "error"); break;
                case "clock_reset": if (phase is not ("stopped" or "failed")) phase = "finished"; logical = null; break;
            }
        }
        var personas = users.Select(user =>
        {
            var log = date == "" || phase == "planning" ? null : Lines(Path.Combine(runs, "days", $"{date}-{user}.jsonl"));
            var run = log?.Skip(log.FindLastIndex(x => Text(x, "event") == "day_start") is var i and >= 0 ? i : log.Count).ToList() ?? [];
            var started = run.Count > 0 && run[0]["host_ms"] is JsonValue sv && sv.TryGetValue<long>(out var ms) ? ms : 0;
            var status = results.TryGetValue(user, out var r) && started <= r.At ? r.Status
                : (run.Any(x => Text(x, "event") == "day_aborted") ? "failed" : run.Any(x => Text(x, "event") == "day_end") ? "done" : run.Count > 0 ? "running" : "waiting");
            var items = run.Count > 0 && run[0]["items"] is JsonValue v && v.TryGetValue<int>(out var n) ? n : 0;
            return new { user, status, items, done = run.Count(x => Text(x, "event") == "item_end") };
        }).ToList();
        if (phase == "preparing" && personas.Any(p => p.status != "waiting")) phase = "running";
        var active = phase is "planning" or "preparing" or "running";
        object? clock = active && DateTimeOffset.TryParse(logical, CultureInfo.InvariantCulture, DateTimeStyles.None, out var l)
            ? new { ms = l.ToUnixTimeMilliseconds() + (now - setAt), offset = (int)l.Offset.TotalMinutes } : null;
        var plans = phase == "planning" && DateTimeOffset.TryParse(logical, CultureInfo.InvariantCulture, DateTimeStyles.None, out var eve)
            && Directory.Exists(Path.Combine(runs, "plans"))
            ? Directory.GetFiles(Path.Combine(runs, "plans"), $"{eve.Date.AddDays(1):yyyy-MM-dd}-*.yaml")
                .Count(f => new DateTimeOffset(File.GetLastWriteTimeUtc(f)).ToUnixTimeMilliseconds() >= setAt)
            : 0;
        return new { phase, day, date, ageMs = Math.Max(0, now - last), clock, unplanned, plans, personas, error };
    }

    static List<JsonObject>? Lines(string path)
    {
        var text = FileText(path);
        if (text is null) return null;
        var result = new List<JsonObject>();
        var lines = text.Split('\n');
        for (var i = 0; i < lines.Length; i++)
        {
            if (string.IsNullOrWhiteSpace(lines[i])) continue;
            if (i == lines.Length - 1 && !text.EndsWith('\n')) break;
            result.Add(JsonNode.Parse(lines[i]) as JsonObject ?? throw new InvalidDataException());
        }
        return result;
    }

    internal static JsonObject[] LastLines(string path, int count)
    {
        string text;
        long start;
        try
        {
            using var f = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            start = Math.Max(0, f.Length - (64 << 10));
            f.Seek(start, SeekOrigin.Begin);
            text = new StreamReader(f).ReadToEnd();
        }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException) { return []; }
        var lines = text.Split('\n').ToList();
        if (start > 0) lines.RemoveAt(0);
        if (!text.EndsWith('\n')) lines.RemoveAt(lines.Count - 1);
        var records = new List<JsonObject>();
        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            try { if (JsonNode.Parse(line) is JsonObject o) records.Add(o); }
            catch (JsonException) { }
        }
        return records.TakeLast(count).ToArray();
    }
}
