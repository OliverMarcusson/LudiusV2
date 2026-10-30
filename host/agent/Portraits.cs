using System.Net.Http.Headers;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Ludius.HostAgent;

static class Portraits
{
    const string Model = "qwen/qwen-image-2.1-turbo";
    const string LogoModel = "openai/gpt-image-1-mini";
    static readonly HttpClient Client = new() { Timeout = TimeSpan.FromMinutes(3), MaxResponseContentBufferSize = 32 << 20 };
    static readonly SemaphoreSlim Gate = new(1);
    static readonly Regex User = new("^[a-z][a-z0-9_-]{0,63}$", RegexOptions.CultureInvariant);

    public static void Map(RouteGroupBuilder api, WebApplication app, string runs, string personas)
    {
        var directory = Path.GetFullPath(app.Configuration["Monitor:Portraits"] ?? Path.Combine(runs, "portraits"), app.Environment.ContentRootPath);
        string? Key()
        {
            if (app.Configuration["OPENROUTER_API_KEY"] is { Length: > 0 } key) return key;
            var file = app.Configuration["Monitor:OpenRouterKeyFile"];
            if (file is null || !File.Exists(file)) return null;
            return File.ReadLines(file).FirstOrDefault(l => l.StartsWith("OPENROUTER_API_KEY="))?
                .Split('=', 2)[1].Trim().Trim('\"', '\'');
        }
        string FileFor(string user) => Path.Combine(directory, user + ".image");
        var logoFile = Path.Combine(directory, "company.logo");
        api.MapGet("/portraits", () => Results.Json(new
        {
            enabled = !string.IsNullOrEmpty(Key()),
            images = Directory.Exists(directory) ? Directory.GetFiles(directory, "*.image")
                .Where(f => User.IsMatch(Path.GetFileNameWithoutExtension(f)))
                .ToDictionary(f => Path.GetFileNameWithoutExtension(f), f => $"/api/portraits/{Path.GetFileNameWithoutExtension(f)}?v={File.GetLastWriteTimeUtc(f).Ticks}") : new Dictionary<string, string>(),
            logo = File.Exists(logoFile) ? $"/api/logo?v={File.GetLastWriteTimeUtc(logoFile).Ticks}" : null
        }));
        api.MapGet("/portraits/{user}", (string user) =>
        {
            if (!User.IsMatch(user) || !File.Exists(FileFor(user))) return Results.NotFound();
            var bytes = File.ReadAllBytes(FileFor(user));
            return Results.File(bytes, Mime(bytes));
        });
        api.MapGet("/logo", () => File.Exists(logoFile) ? Results.File(File.ReadAllBytes(logoFile), "image/png") : Results.NotFound());

        IResult? Refused(HttpContext ctx)
        {
            var origin = ctx.Request.Headers.Origin.ToString();
            var allowed = Monitor.ParseOrigins(app.Configuration["Monitor:AllowedOrigins"]);
            if (origin.Length == 0 || !Monitor.OriginAllowed(origin, ctx.Request.Host.Value, allowed)) return Results.StatusCode(403);
            return ctx.Request.HasJsonContentType() ? null : Results.StatusCode(415);
        }
        async Task<IResult> Generate(string target, object payload, bool pngOnly)
        {
            var key = Key();
            if (string.IsNullOrWhiteSpace(key)) return Results.Problem("OpenRouter image generation is not configured.", statusCode: 503);
            using var request = new HttpRequestMessage(HttpMethod.Post, "https://openrouter.ai/api/v1/images");
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", key);
            request.Content = JsonContent.Create(payload);
            using var response = await Client.SendAsync(request, app.Lifetime.ApplicationStopping);
            if (!response.IsSuccessStatusCode) return Results.Problem($"OpenRouter image generation failed (HTTP {(int)response.StatusCode}).", statusCode: 502);
            var body = JsonNode.Parse(await response.Content.ReadAsStringAsync());
            var encoded = (body?["data"] as JsonArray)?.FirstOrDefault()?["b64_json"]?.GetValue<string>();
            if (encoded is null) return Results.Problem("OpenRouter returned no image. The existing image was kept.", statusCode: 502);
            var image = Convert.FromBase64String(encoded);
            var mime = Mime(image);
            if (image.Length > 16 << 20 || mime == "application/octet-stream" || (pngOnly && mime != "image/png"))
                return Results.Problem("OpenRouter returned an unsupported image.", statusCode: 502);
            Directory.CreateDirectory(directory);
            var temp = target + ".tmp";
            await File.WriteAllBytesAsync(temp, image, app.Lifetime.ApplicationStopping);
            File.Move(temp, target, overwrite: true);
            return Results.Ok(new { saved = true });
        }
        async Task<IResult> Guarded(Func<Task<IResult>> work, string what)
        {
            if (!await Gate.WaitAsync(0)) return Results.Conflict(new { error = "Another image is being generated. Try again when it finishes." });
            try { return await work(); }
            catch (Exception e) when (e is HttpRequestException or TaskCanceledException or System.Text.Json.JsonException or FormatException or InvalidOperationException)
            { return Results.Problem($"{what} generation failed. The existing image was kept; try again.", statusCode: 502); }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException or YamlDotNet.Core.YamlException)
            { return Results.Problem($"{what} storage or persona data is unavailable.", statusCode: 503); }
            finally { Gate.Release(); }
        }

        api.MapPost("/portraits/{user}", async (string user, HttpContext ctx) =>
        {
            if (Refused(ctx) is { } refused) return refused;
            if (!User.IsMatch(user)) return Results.BadRequest();
            return await Guarded(async () =>
            {
                var catalog = System.Text.Json.JsonSerializer.SerializeToNode(ConsoleData.Catalog(runs, personas))!;
                var person = catalog["people"]?.AsArray().FirstOrDefault(p => p?["user"]?.ToString() == user)
                    ?? catalog["company"]?["people"]?.AsArray().FirstOrDefault(p => p?["user"]?.ToString() == user);
                if (person is null) return Results.NotFound();
                if (File.Exists(FileFor(user)) && ctx.Request.Query["replace"] != "true") return Results.Ok(new { saved = true });
                var prompt = $"Create a realistic staff-directory portrait of one fictional adult employee: {person["name"]}, {person["role"]}, {person["department"]}, at {catalog["company"]?["company"]} in {catalog["company"]?["city"]}. "
                    + "Head and shoulders, looking at the camera, natural expression, ordinary work clothing appropriate to the role, soft daylight, plain muted grey office background. Square crop, face clearly visible. Candid corporate directory photography, realistic skin texture. No text, lettering, logo, watermark or collage. This is an invented character, not a real person.";
                return await Generate(FileFor(user), new { model = Model, prompt, n = 1, aspect_ratio = "1:1", resolution = "1K" }, pngOnly: false);
            }, "Portrait");
        });
        api.MapPost("/logo", async (HttpContext ctx) =>
        {
            if (Refused(ctx) is { } refused) return refused;
            return await Guarded(async () =>
            {
                var company = System.Text.Json.JsonSerializer.SerializeToNode(ConsoleData.Catalog(runs, personas))!["company"];
                if (company?["company"] is null) return Results.NotFound();
                if (File.Exists(logoFile) && ctx.Request.Query["replace"] != "true") return Results.Ok(new { saved = true });
                var summary = company["summary"]?.ToString() ?? "";
                if (summary.Length > 400) summary = summary[..400];
                var prompt = $"Design a logo emblem for {company["company"]}, a fictional company in {company["city"]}. About the company: {summary} "
                    + "A single simple, memorable symbol that hints at what the company makes. Flat vector style, two or three solid colours of medium brightness so it reads on both dark and light backgrounds, clean geometric shapes, centred with generous margin. "
                    + "Transparent background: no backdrop, frame, badge, shadow or mockup. No text, letters or numbers.";
                return await Generate(logoFile, new { model = LogoModel, prompt, n = 1, aspect_ratio = "1:1", background = "transparent", quality = "medium" }, pngOnly: true);
            }, "Logo");
        });
    }

    static string Mime(byte[] bytes) => bytes.AsSpan().StartsWith(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 }) ? "image/png"
        : bytes.AsSpan().StartsWith(new byte[] { 255, 216, 255 }) ? "image/jpeg"
        : bytes.Length >= 12 && System.Text.Encoding.ASCII.GetString(bytes, 0, 4) == "RIFF" && System.Text.Encoding.ASCII.GetString(bytes, 8, 4) == "WEBP" ? "image/webp"
        : "application/octet-stream";
}
