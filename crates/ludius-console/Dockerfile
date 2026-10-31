# Build context: the repo root (docker build -f crates/ludius-console/Dockerfile .)
FROM oven/bun:1 AS web
WORKDIR /web
COPY web/console/package.json web/console/bun.lock ./
RUN bun install --frozen-lockfile
COPY web/console ./
RUN bun run build

FROM rust:1-trixie AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY proto proto
COPY crates crates
RUN cargo build --release --locked -p ludius-console

FROM gcr.io/distroless/cc-debian13
COPY --from=build /src/target/release/ludius-console /ludius-console
COPY --from=web /web/dist /srv/monitor
ENTRYPOINT ["/ludius-console"]
