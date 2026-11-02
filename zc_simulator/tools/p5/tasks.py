"""Versioned task package reader for the simulator-owned demo extension."""
import hashlib
import json
from pathlib import Path


class TaskPackage:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.spec = json.loads((self.directory/'task.json').read_text())
        if self.spec.get('schema') != 1 or self.spec.get('kind') != 'existing-tensor':
            raise ValueError('unsupported task package schema/kind')
        def member(name):
            p = (self.directory/name).resolve()
            if self.directory not in p.parents or not p.is_file():
                raise ValueError('package member must stay inside package directory')
            return p
        pins = self.spec.get('sha256', {})
        for key in ('program', 'input', 'parameters', 'expected'):
            name = self.spec[key]
            if name not in pins:
                raise ValueError('unpinned package member: '+name)
            p = member(name)
            if hashlib.sha256(p.read_bytes()).hexdigest() != pins[name]:
                raise ValueError('package digest mismatch: '+name)
        self.data = member(self.spec['input']).read_bytes()
        self.parameters = member(self.spec['parameters']).read_bytes()
        self.expected = member(self.spec['expected']).read_bytes()
        if len(self.data) != 0x9200 or len(self.parameters) != 128 or len(self.expected) != 33792:
            raise ValueError('existing Tensor task dimensions/parameters')
