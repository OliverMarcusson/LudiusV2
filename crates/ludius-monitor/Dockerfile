# Build context: the repo root (docker build -f crates/ludius-monitor/Dockerfile .)
FROM oven/bun:1 AS web
WORKDIR /web
COPY web/monitor/package.json web/monitor/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/monitor ./
RUN bun run build

FROM rust:1-trixie AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY proto proto
COPY crates crates
RUN cargo build --release --locked -p ludius-monitor

FROM gcr.io/distroless/cc-debian13
COPY --from=build /src/target/release/ludius-monitor /ludius-monitor
COPY --from=web /web/dist /srv/monitor
ENTRYPOINT ["/ludius-monitor"]
