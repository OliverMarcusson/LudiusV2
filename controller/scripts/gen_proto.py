import pathlib

from grpc_tools import protoc

here = pathlib.Path(__file__).resolve().parent.parent
proto = here.parent / "proto"
out = here / "src"
files = [p.relative_to(proto).as_posix() for p in (proto / "ludius" / "v1").glob("*.proto")]
args = ["protoc", f"-I{proto}", f"--python_out={out}", f"--pyi_out={out}", f"--grpc_python_out={out}", *files]
if protoc.main(args) != 0:
    raise SystemExit("protoc failed")
(out / "ludius" / "v1" / "__init__.py").touch()
