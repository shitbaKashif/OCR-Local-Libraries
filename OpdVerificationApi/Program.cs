using OpdVerificationApi.Data;
using OpdVerificationApi.Services;
using OpdVerificationApi.Settings;
using Microsoft.AspNetCore.Mvc;

var builder = WebApplication.CreateBuilder(args);

// Controllers — suppress automatic 400; controller validates after auth check
builder.Services.AddControllers()
    .ConfigureApiBehaviorOptions(options =>
    {
        options.SuppressModelStateInvalidFilter = true;
    });

// HttpClient for Python sidecar
var sidecarConfig = builder.Configuration.GetSection("PythonSidecar");
var baseUrl       = sidecarConfig["BaseUrl"] ?? "http://127.0.0.1:8001";
var timeoutSec    = sidecarConfig.GetValue<int>("TimeoutSeconds", 30);

builder.Services.AddHttpClient<ImageIntelClient>(client =>
{
    client.BaseAddress = new Uri(baseUrl);
    client.Timeout     = TimeSpan.FromSeconds(timeoutSec);
});

// Application services
builder.Services.AddSingleton<TokenAuthService>();
builder.Services.AddScoped<AttIndexRepository>();
builder.Services.Configure<DupDetectionSettings>(
    builder.Configuration.GetSection("DupDetection"));

// Logging
builder.Services.AddLogging(logging =>
{
    logging.AddConsole();
    logging.AddDebug();
});

builder.WebHost.UseUrls("http://127.0.0.1:5000");

var app = builder.Build();

// Exception handler must be first in the pipeline
app.UseExceptionHandler(err =>
{
    err.Run(async ctx =>
    {
        ctx.Response.StatusCode  = 500;
        ctx.Response.ContentType = "application/json";
        await ctx.Response.WriteAsJsonAsync(new { error = "Internal server error" });
    });
});

app.MapControllers();
app.MapGet("/health", () => Results.Ok(new { status = "ok" }));

app.Run();
