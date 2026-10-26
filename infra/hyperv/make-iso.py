"""Write a one-file ISO holding autounattend.xml: make-iso.py <xml> <out.iso>."""

import sys

import pycdlib

iso = pycdlib.PyCdlib()
iso.new(interchange_level=3, joliet=3, vol_ident="ANSWER")
iso.add_file(sys.argv[1], "/AUTOUNATTEND.XML;1", joliet_path="/autounattend.xml")
iso.write(sys.argv[2])
iso.close()
