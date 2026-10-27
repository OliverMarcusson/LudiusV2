"""Write a cloud-init NoCloud seed ISO (volume CIDATA): make-seed.py <out.iso> <file>... (user-data, meta-data, ...)."""

import sys
from pathlib import Path

import pycdlib

iso = pycdlib.PyCdlib()
iso.new(interchange_level=3, joliet=3, rock_ridge="1.09", vol_ident="CIDATA")
for f in sys.argv[2:]:
    name = Path(f).name
    iso.add_file(f, f"/{name.upper().replace('-', '_')}.;1", joliet_path=f"/{name}", rr_name=name)
iso.write(sys.argv[1])
iso.close()
