"""Shared test helpers."""
import re

LINE = re.compile(r'^(?P<key>[^, ]+),(?P<dims>.*) (?P<type>gauge),min=(?P<min>[^,]+),max=(?P<max>[^,]+),'
                  r'sum=(?P<sum>[^,]+),count=(?P<count>[^ ]+) (?P<ts>\d+)$')


def parse(line):
    m = LINE.match(line)
    assert m, f"not valid line protocol: {line}"
    dims = dict(re.findall(r'([a-z0-9_.-]+)="((?:[^"\\]|\\.)*)"', m["dims"]))
    return {**m.groupdict(), "dims": dims}
