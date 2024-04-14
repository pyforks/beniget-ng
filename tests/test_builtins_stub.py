from unittest import TestCase, skipIf
import pathlib
import sys
import ast as _ast
import gast as _gast

from .test_chains import getDefUseChainsType

testmodules = pathlib.Path(__file__).parent / 'testmodules'

class TestBuiltinsStubs(TestCase):
    ast = _gast

    @skipIf(sys.version_info < (3, 8), reason='positional only syntax is used')
    def test_buitlins_stub(self):
        file = testmodules / 'builtins.pyi'
        filename = file.as_posix()
        node = self.ast.parse(file.read_text(), filename)
        c = getDefUseChainsType(node)(filename)
        c.visit(node)
        
        # all builtins references are sucessfuly linked to their definition
        # in that module and not the default builtins.
        for chains in c._builtins.values():
            assert not chains.users(), chains


class TestBuiltinsStubsStdlib(TestBuiltinsStubs):
    ast = _ast