# Build context: the repo root (docker build -f crates/ludius-host-agent/Dockerfile .)
FROM rust:1-trixie AS build
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY proto proto
COPY crates crates
RUN cargo build --release --locked -p ludius-host-agent

FROM gcr.io/distroless/cc-debian13
COPY --from=build /src/target/release/ludius-host-agent /ludius-host-agent
ENTRYPOINT ["/ludius-host-agent"]
