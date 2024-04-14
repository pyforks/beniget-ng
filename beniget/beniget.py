from collections import defaultdict, deque
from contextlib import contextmanager
import sys
import os.path

import ast as _ast
import gast as ast

from .ordered_set import ordered_set

_ClassOrFunction = set(('ClassDef', 'FunctionDef', 'AsyncFunctionDef'))
_Comp = set(('DictComp', 'ListComp', 'SetComp', 'GeneratorExp'))
_ClosedScopes = set(('FunctionDef', 'AsyncFunctionDef',
                  'Lambda', 'DictComp', 'ListComp',
                  'SetComp', 'GeneratorExp', 'def695'))
_TypeVarLike = set(('TypeVar', 'TypeVarTuple', 'ParamSpec'))
_HasName = set((*_ClassOrFunction, *_TypeVarLike))

class Ancestors(ast.NodeVisitor):
    """
    Build the ancestor tree, that associates a node to the list of node visited
    from the root node (the Module) to the current node
    >>> import gast as ast
    >>> code = 'def foo(x): return x + 1'
    >>> module = ast.parse(code)
    >>> from beniget import Ancestors
    >>> ancestors = Ancestors()
    >>> ancestors.visit(module)
    >>> binop = module.body[0].body[0].value
    >>> for n in ancestors.parents(binop):
    ...    print(type(n))
    <class 'gast.gast.Module'>
    <class 'gast.gast.FunctionDef'>
    <class 'gast.gast.Return'>
    
    Also works with standard library nodes
    
    >>> import ast as _ast
    >>> code = 'def foo(x): return x + 1'
    >>> module = _ast.parse(code)
    >>> from beniget import Ancestors
    >>> ancestors = Ancestors()
    >>> ancestors.visit(module)
    >>> binop = module.body[0].body[0].value
    >>> for n in ancestors.parents(binop):
    ...    print(str(type(n)).replace('_ast', 'ast'))
    <class 'ast.Module'>
    <class 'ast.FunctionDef'>
    <class 'ast.Return'>
    """

    def __init__(self):
        self._parents = dict()
        self._current = list()

    def generic_visit(self, node):
        self._parents[node] = list(self._current)
        self._current.append(node)
        super().generic_visit(node)
        self._current.pop()

    def parent(self, node):
        return self._parents[node][-1]

    def parents(self, node):
        return self._parents[node]

    def parentInstance(self, node, cls):
        for n in reversed(self._parents[node]):
            if isinstance(n, cls):
                return n
        raise ValueError("{} has no parent of type {}".format(node, cls))

    def parentFunction(self, node):
        return self.parentInstance(node, (ast.FunctionDef,
                                          ast.AsyncFunctionDef,
                                          _ast.FunctionDef, 
                                          _ast.AsyncFunctionDef))

    def parentStmt(self, node):
        return self.parentInstance(node, _ast.stmt)

class ImportInfo:
    """
    Complement an `ast.alias` node with resolved 
    origin module and name of the locally bound name.

    :note: `orgname` will be ``*`` for wildcard imports.
    """
    __slots__ = 'orgmodule', 'orgname'

    def __init__(self, orgmodule, orgname=None):
        """
        :param orgmodule: str
        :param orgname: str or None
        """
        self.orgmodule = orgmodule
        self.orgname = orgname
    
    def target(self):
        """
        Returns the qualified name of the the imported symbol, str.
        """
        if self.orgname:
            return "{}.{}".format(self.orgmodule, self.orgname)
        else:
            return self.orgmodule


# The MIT License (MIT)
# Copyright (c) 2017 Jelle Zijlstra
# Adapted from the project typeshed_client.
def parse_import(node, modname, is_package=False):
    """
    Parse the given import node into a mapping of aliases to `ImportInfo`.
    
    :param node: the import node.
    :param str modname: the name of the module.
    :param bool is_package: whether the module is a package.
    :rtype: dict[ast.alias, ImportInfo]
    """
    result = {}

    typename = type(node).__name__
    if typename == 'Import':
        for al in node.names:
            if al.asname:
                result[al] = ImportInfo(orgmodule=al.name)
            else:
                # Here, we're not including information 
                # regarding the submodules imported - if there is one.
                # This is because this analysis map the names bounded by imports, 
                # not the dependencies.
                result[al] = ImportInfo(orgmodule=al.name.split(".", 1)[0])
    
    elif typename == 'ImportFrom':
        current_module = tuple(modname.split("."))

        if node.module is None:
            module = ()
        else:
            module = tuple(node.module.split("."))
        
        if not node.level:
            source_module = module
        else:
            # parse relative imports
            if node.level == 1:
                if is_package:
                    relative_module = current_module
                else:
                    relative_module = current_module[:-1]
            else:
                if is_package:
                    relative_module = current_module[: 1 - node.level]
                else:
                    relative_module = current_module[: -node.level]

            if not relative_module:
                # We don't raise errors when an relative import makes no sens, 
                # we simply pad the name with dots.
                relative_module = ("",) * node.level

            source_module = relative_module + module

        for alias in node.names:
            result[alias] = ImportInfo(
                orgmodule=".".join(source_module), orgname=alias.name
            )

    else:
        raise TypeError('unexpected node type: {}'.format(type(node)))
    
    return result

_novalue = object()
@contextmanager
def _setattrs(obj, **attrs):
    """
    Provide cheap attribute polymorphism.
    """
    old_values = {}
    for k, v in attrs.items():
        old_values[k] = getattr(obj, k, _novalue)
        setattr(obj, k, v)
    yield 
    for k, v in old_values.items():
        if v is _novalue:
            delattr(obj, k)
        else:
            setattr(obj, k, v)

class Def(object):
    """
    Model a definition, either named or unnamed, and its users.
    """

    __slots__ = "node", "_users", "islive"

    def __init__(self, node):
        self.node = node
        self._users = ordered_set()
        self.islive = True
        """
        Whether this definition might reach the final block of it's scope.
        Meaning if islive is `False`, the definition will always be overriden
        at the time we finished executing the module/class/function body.
        So the definition could be ignored in the context of an attribute access for instance.
        """

    def add_user(self, node):
        assert isinstance(node, Def), node
        self._users.add(node)

    def name(self):
        """
        If the node associated to this Def has a name, returns this name.
        Otherwise returns its type
        """
        typename = type(self.node).__name__
        if typename in _HasName:
            return self.node.name
        elif typename == 'Name':
            return self.node.id
        elif typename == 'alias':
            base = self.node.name.split(".", 1)[0]
            return self.node.asname or base
        elif typename in ('MatchStar', 'MatchAs') and self.node.name:
            return self.node.name
        elif typename == 'MatchMapping' and self.node.rest:
            return self.node.rest
        elif typename == 'arg':
            return self.node.arg
        elif typename == 'ExceptHandler' and self.node.name:
            return self.node.name
        elif isinstance(self.node, tuple):
            return self.node[1]
        return typename

    def users(self):
        """
        The list of ast entity that holds a reference to this node
        """
        return self._users

    def __repr__(self):
        return self._repr({})

    def _repr(self, nodes):
        if self in nodes:
            return "(#{})".format(nodes[self])
        else:
            nodes[self] = len(nodes)
            return "{} -> ({})".format(
                self.node, ", ".join(u._repr(nodes.copy())
                                     for u in self._users)
            )

    def __str__(self):
        return self._str({})

    def _str(self, nodes):
        if self in nodes:
            return "(#{})".format(nodes[self])
        else:
            nodes[self] = len(nodes)
            return "{} -> ({})".format(
                self.name(), ", ".join(u._str(nodes.copy())
                                       for u in self._users)
            )


import builtins
BuiltinsSrc = builtins.__dict__

Builtins = {k: v for k, v in BuiltinsSrc.items()}

Builtins["__file__"] = __file__

DeclarationStep, DefinitionStep = object(), object()

def collect_future_imports(node):
    """
    Returns a set of future imports names for the given ast module.
    """
    assert type(node).__name__ == 'Module'
    cf = _CollectFutureImports()
    cf.visit(node)
    return cf.FutureImports

class _StopTraversal(Exception):
    pass

class _CollectFutureImports(ast.NodeVisitor):
    # A future statement must appear near the top of the module.
    # The only lines that can appear before a future statement are:
    # - the module docstring (if any),
    # - comments,
    # - blank lines, and
    # - other future statements.
    # as soon as we're visiting something else, we can stop the visit.
    def __init__(self):
        self.FutureImports = set() #type:set[str]

    def visit_Module(self, node):
        for child in node.body:
            try:
                self.visit(child)
            except _StopTraversal:
                break

    def visit_ImportFrom(self, node):
        if node.level or node.module != '__future__':
            raise _StopTraversal()
        self.FutureImports.update((al.name for al in node.names))

    def visit_Expr(self, node):
        self.visit(node.value)

    def visit_Constant(self, node):
        if not isinstance(node.value, str):
            raise _StopTraversal()

    def generic_visit(self, node):
        raise _StopTraversal()

    def visit_Str(self, node):
        pass

class CollectLocals(ast.NodeVisitor):
    def __init__(self):
        self.Locals = set()
        self.NonLocals = set()

    def visit_FunctionDef(self, node):
        # no recursion
        self.Locals.add(node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    visit_ClassDef = visit_FunctionDef

    def visit_Nonlocal(self, node):
        self.NonLocals.update(name for name in node.names)

    visit_Global = visit_Nonlocal

    def visit_Name(self, node):
        if type(node.ctx).__name__ == 'Store' and node.id not in self.NonLocals:
            self.Locals.add(node.id)

    def skip(self, _):
        pass

    visit_SetComp = visit_DictComp = visit_ListComp = skip
    visit_GeneratorExp = skip

    visit_Lambda = skip

    def visit_Import(self, node):
        for alias in node.names:
            base = alias.name.split(".", 1)[0]
            self.Locals.add(alias.asname or base)

    def visit_ImportFrom(self, node):
        for alias in node.names:
            self.Locals.add(alias.asname or alias.name)

class CollectLocalsdef695(CollectLocals):
    
    visit_TypeVar = visit_ParamSpec = visit_TypeVarTuple = CollectLocals.visit_FunctionDef

def collect_locals(node):
    '''
    Compute the set of identifiers local to a given node.

    This is meant to emulate a call to locals()
    '''
    if isinstance(node, def695):
        # workaround for the new implicit scope created by type params and co.
        visitor = CollectLocalsdef695()
    else:
        visitor = CollectLocals()
    visitor.generic_visit(node)
    return visitor.Locals

class def695(ast.stmt):
    """
    Special statement to represent the PEP-695 lexical scopes.
    """
    _fields = ('body', 'd')
    def __init__(self, body, d):
        self.body = body # list of type params
        self.d = d # the wrapped definition node
        
def posixpath_splitparts(path):
    """
    Split a POSIX filename in parts.

    >>> posixpath_splitparts('typing.pyi')
    ('typing.pyi',)
    
    >>> posixpath_splitparts('/var/lib/config.ini')
    ('var', 'lib', 'config.ini')

    >>> posixpath_splitparts('/var/lib/config/')
    ('var', 'lib', 'config')

    >>> posixpath_splitparts('c:/dir/config.ini')
    ('c:', 'dir', 'config.ini')
    """
    sep = '/'
    r = deque(path.split(sep))
    # make sure the parts doesn't 
    # start or ends with a separator or empty string.
    while r and r[0] in (sep, ''):
        r.popleft()
    while r and r[-1] in (sep, ''):
        r.pop()
    return tuple(r)

def potential_module_names(filename):
    """
    Returns a tuple of potential module 
    names deducted from the filename.

    >>> potential_module_names('/var/lib/config.py')
    ('var.lib.config', 'lib.config', 'config')
    >>> potential_module_names('git-repos/pydoctor/pydoctor/driver.py')
    ('pydoctor.pydoctor.driver', 'pydoctor.driver', 'driver')
    >>> potential_module_names('git-repos/pydoctor/pydoctor/__init__.py')
    ('pydoctor.pydoctor', 'pydoctor')
    """
    parts = posixpath_splitparts(filename)
    mod = os.path.splitext(parts[-1])[0]
    if mod == '__init__':
        parts = parts[:-1]
    else:
        parts = parts[:-1] + (mod,)
    
    names = []
    len_parts = len(parts)
    for i in range(len_parts):
        p = parts[i:]
        if not p or any(not all(sb.isidentifier() 
                        for sb in s.split('.')) for s in p):
            # the path cannot be converted to a module name
            # because there are unallowed caracters.
            continue
        names.append('.'.join(p))
    
    return tuple(names) or ('',)


def matches_qualname(heads, locals, imports, modnames, expr, qnames):
    """
    Returns True if - one of - the expression's definition(s) matches
    one of the given qualified names.

    The expression definition is looked up with 
    `lookup_annotation_name_defs`.

    :param heads: The current scopes.
    :param locals: The locals mapping.
    :param imports: The mapping of resolved imports.
    :param modnames: A collection containing the name of the current module. 
    :param expr: The name/attribute expression to match.
    :param qnames: A collection of qualified names to look for.
    """
    
    typename = type(expr).__name__
    if typename == 'Name':
        try:
            defs = lookup_annotation_name_defs(expr.id, heads, locals)
        except Exception:
            return False
        
        for d in defs:
            if type(d.node).__name__ == 'alias':
                # the symbol is an imported name
                import_alias = imports[d.node].target()
                if any(import_alias == n for n in qnames):
                    return True
            elif any('{}.{}'.format(mod, d.name()) in qnames for mod in modnames):
                # the symbol is a localy defined name
                return True
            else:
                # localy defined name, but module name doesn't match
                break

    elif typename == 'Attribute':
        for n in qnames:
            mod, _, _name = n.rpartition('.')
            if mod and expr.attr == _name:
                if matches_qualname(heads, locals, imports, modnames, expr.value, set((mod,))):
                    return True
    return False

def matches_typing_name(heads, locals, imports, modnames, expr, name):
    return matches_qualname(heads, locals, imports, modnames, expr, 
                            set(('typing.{}'.format(name), 
                                 'typing_extensions.{}'.format(name))))

class DefUseChains(ast.NodeVisitor):
    """
    Module visitor that gathers two kinds of informations:
        - locals: dict[node, list[Def]], a mapping between a node and the list
          of variable defined in this node,
        - chains: dict[node, Def], a mapping between nodes and their chains.
        - imports: dict[node, ImportInfo], a mapping between import aliases
          and their resolved target.

    >>> import gast as ast
    >>> module = ast.parse("from b import c, d; c()")
    >>> duc = DefUseChains()
    >>> duc.visit(module)
    >>> for head in duc.locals[module]:
    ...     print("{}: {}".format(head.name(), len(head.users())))
    c: 1
    d: 0
    >>> alias_def = duc.chains[module.body[0].names[0]]
    >>> print(alias_def)
    c -> (c -> (Call -> ()))

    One instance of DefUseChains is only suitable to analyse one AST Module in it's lifecycle.
    """

    def __init__(self,
                 filename=None,
                 modname=None,
                 future_annotations=False, 
                 is_stub=False):
        """
            - filename: str, POSIX-like path pointing to the source file, 
              you can use `Path.as_posix` to ensure the value has proper format. 
              It's recommended to either provide the filename of the source
              relative to the root of the package or provide both 
              a module name and a filename.
              Included in error messages and used as part of the import resolving.
            - modname: str, fully qualified name of the module we're analysing. 
              A module name may end with '.__init__' to indicate the module is a package.
            - future_annotations: bool, PEP 563 mode. 
              It will auotmatically be enabled if the module has ``from __future__ import annotations``.
            - is_stub: bool, stub module semantics mode, implies future_annotations=True.
              It will auotmatically be enabled if the filename endswith '.pyi'.
              When the module is a stub file, there is no need for quoting to do a forward reference 
              inside: 
                - annotations (like PEP 563 mode)
                - `TypeAlias`` values
                - ``TypeVar()`` call arguments
                - classe base expressions, keywords and decorators
                - function decorators
        """
        self.chains = {}
        self.locals = defaultdict(list)
        # mapping from ast.alias to their ImportInfo.
        self.imports = {}

        self.filename = filename
        self.is_stub = is_stub or filename is not None and filename.endswith('.pyi')
        
        # determine module name, we provide some flexibility: 
        # - The module name is not required to have correct parsing when the 
        #   filename is a relative filename that starts at the package root. 
        # - We deduce whether the module is a package from module name or filename
        #   if they ends with __init__.
        # - The module name doesn't have to be provided to use matches_qualname() 
        #   if filename is provided.
        is_package = False
        if filename and posixpath_splitparts(filename)[-1].split('.')[0] == '__init__':
            is_package = True
        if modname:
            if modname.endswith('.__init__'):
                modname = modname[:-9] # strip __init__
                is_package = True
            self._modnames = (modname, )
        elif filename:
            self._modnames = potential_module_names(filename)
        else:
            self._modnames = ('', )
        self.modname = next(iter(self._modnames))
        self.is_package = is_package

        # deep copy of builtins, to remain reentrant
        self._builtins = {k: Def(v) for k, v in Builtins.items()}

        # function body are not executed when the function definition is met
        # this holds a list of the functions met during body processing
        self._defered = []

        # stack of mapping between an id and Names
        self._definitions = []

        # stack of scope depth
        self._scope_depths = []

        # stack of variable defined with the global keywords
        self._globals = []

        # stack of local identifiers, used to detect 'read before assign'
        self._precomputed_locals = []

        # stack of variable that were undefined when we met them, but that may
        # be defined in another path of the control flow (esp. in loop)
        self._undefs = []

        # stack of nodes starting a scope: 
        # class, module, function, generator expression, 
        # comprehension, def695. 
        self._scopes = []

        self._breaks = []
        self._continues = []

        # stack of list of annotations (annotation, heads, callback),
        # only used in the case of from __future__ import annotations feature.
        # the annotations are analyzed when the whole module has been processed,
        # it should be compatible with PEP 563, and minor changes are required to support PEP 649.
        self._defered_annotations = []

        # dead code levels, it's non null for code that cannot be executed
        self._deadcode = 0

        # attributes set in visit_Module
        self.module = None
        self.future_annotations = self.is_stub or future_annotations

    #
    ## helpers
    #

    def _dump_locals(self, node, only_live=False):
        """
        Like `dump_definitions` but returns the result grouped by symbol name and it includes linenos.

        :Returns: List of string formatted like: '{symbol name}:{def lines}'
        """
        groupped = defaultdict(list)
        for d in self.locals[node]:
            if not only_live or d.islive:
                groupped[d.name()].append(d)
        return ['{}:{}'.format(name, ','.join([str(getattr(d.node, 'lineno', None)) for d in defs])) \
            for name,defs in groupped.items()]

    def dump_definitions(self, node, ignore_builtins=True):
        if type(node).__name__ == 'Module' and not ignore_builtins:
            builtins = {d for d in self._builtins.values()}
            return sorted(d.name()
                          for d in self.locals[node] if d not in builtins)
        else:
            return sorted(d.name() for d in self.locals[node])

    def dump_chains(self, node):
        chains = []
        for d in self.locals[node]:
            chains.append(str(d))
        return chains

    def location(self, node):
        if hasattr(node, "lineno"):
            filename = "{}:".format(
                "<unknown>" if self.filename is None else self.filename
            )
            return " at {}{}:{}".format(filename,
                                            node.lineno,
                                            getattr(node, 'col_offset', None),)
        else:
            return ""

    def unbound_identifier(self, name, node):
        self.warn("unbound identifier '{}'".format(name), node)
    
    def warn(self, msg, node):
        print("W: {}{}".format(msg, self.location(node)))

    def invalid_name_lookup(self, name, scope, precomputed_locals, local_defs):
        # We may hit the situation where we refer to a local variable which is
        # not bound yet. This is a runtime error in Python, so we try to detec
        # it statically.

        # not a local variable => fine
        if name not in precomputed_locals:
            return

        # It's meant to be a local, but can we resolve it by a local lookup?
        islocal = any((name in defs or '*' in defs) for defs in local_defs)

        # At class scope, it's ok to refer to a global even if we also have a
        # local definition for that variable. Stated other wise
        #
        # >>> a = 1
        # >>> def foo(): a = a
        # >>> foo() # fails, a is a local referenced before being assigned
        # >>> class bar: a = a
        # >>> bar() # ok, and `bar.a is a`
        if type(scope).__name__ in ('ClassDef', 'def695'):  # TODO: test the def695 part of this
            top_level_definitions = self._definitions[0:-self._scope_depths[0]]
            isglobal = any((name in top_lvl_def or '*' in top_lvl_def)
                           for top_lvl_def in top_level_definitions)
            return not islocal and not isglobal
        else:
            return not islocal

    def compute_annotation_defs(self, node, quiet=False):
        name = node.id
        # resolving an annotation is a bit different
        # form other names.
        try:
            return lookup_annotation_name_defs(name, self._scopes, self.locals)
        except LookupError:
            # fallback to regular behaviour on module scope
            # to support names from builtins or wildcard imports.
            return self.compute_defs(node, quiet=quiet)

    def compute_defs(self, node, quiet=False):
        '''
        Performs an actual lookup of node's id in current context, returning
        the list of def linked to that use.
        '''
        name = node.id
        stars = []

        # If the `global` keyword has been used, honor it
        if any(name in _globals for _globals in self._globals):
            looked_up_definitions = self._definitions[0:-self._scope_depths[0]]
        else:
            # List of definitions to check. This includes all non-class
            # definitions *and* the last definition. Class definitions are not
            # included because they require fully qualified access.
            looked_up_definitions = []

            scopes_iter = iter(reversed(self._scopes))
            depths_iter = iter(reversed(self._scope_depths))
            precomputed_locals_iter = iter(reversed(self._precomputed_locals))

            # Keep the last scope because we could be in class scope, in which
            # case we don't need fully qualified access.
            lvl = depth = next(depths_iter)
            precomputed_locals = next(precomputed_locals_iter)
            base_scope = next(scopes_iter)
            defs = self._definitions[depth:]
            is_def695 = isinstance(base_scope, def695)
            if not self.invalid_name_lookup(name, base_scope, precomputed_locals, defs):
                looked_up_definitions.extend(reversed(defs))

                # Iterate over scopes, filtering out class scopes.
                for scope, depth, precomputed_locals in zip(scopes_iter,
                                                            depths_iter,
                                                            precomputed_locals_iter):
                    # If a def695 scope is immediately within a class scope, or within another def695 scope that is immediately within a class scope, 
                    # then names defined in that class scope can be accessed within the def695 scope. 
                    if type(scope).__name__ != 'ClassDef' or is_def695:
                        defs = self._definitions[lvl + depth: lvl]
                        if self.invalid_name_lookup(name, base_scope, precomputed_locals, defs):
                            looked_up_definitions.append(StopIteration)
                            break
                        looked_up_definitions.extend(reversed(defs))
                    lvl += depth

        for defs in looked_up_definitions:
            if defs is StopIteration:
                break
            elif name in defs:
                return defs[name] if not stars else stars + list(defs[name])
            elif "*" in defs:
                stars.extend(defs["*"])

        d = self.chains.setdefault(node, Def(node))

        if self._undefs:
            self._undefs[-1][name].append((d, stars))

        if stars:
            return stars + [d]
        else:
            if not self._undefs and not quiet:
                self.unbound_identifier(name, node)
            return [d]

    defs = compute_defs

    def process_body(self, stmts):
        deadcode = False
        for stmt in stmts:
            self.visit(stmt)
            if type(stmt).__name__ in ('Break', 'Continue', 'Raise'):
                if not deadcode:
                    deadcode = True
                    self._deadcode += 1
        if deadcode:
            self._deadcode -= 1

    def process_undefs(self):
        for undef_name, _undefs in self._undefs[-1].items():
            if undef_name in self._definitions[-1]:
                for newdef in self._definitions[-1][undef_name]:
                    for undef, _ in _undefs:
                        for user in undef.users():
                            newdef.add_user(user)
            else:
                for undef, stars in _undefs:
                    if not stars:
                        self.unbound_identifier(undef_name, undef.node)
        self._undefs.pop()

    @contextmanager
    def ScopeContext(self, node):
        self._scopes.append(node)
        self._scope_depths.append(-1)
        self._definitions.append(defaultdict(ordered_set))
        self._globals.append(set())
        self._precomputed_locals.append(collect_locals(node))
        yield
        self._precomputed_locals.pop()
        self._globals.pop()
        self._definitions.pop()
        self._scope_depths.pop()
        self._scopes.pop()

    CompScopeContext = ScopeContext

    @contextmanager
    def DefinitionContext(self, definitions):
        self._definitions.append(definitions)
        self._scope_depths[-1] -= 1
        yield self._definitions[-1]
        self._scope_depths[-1] += 1
        self._definitions.pop()


    @contextmanager
    def SwitchScopeContext(self, defs, scopes, scope_depths, precomputed_locals):
        scope_depths, self._scope_depths = self._scope_depths, scope_depths
        scopes, self._scopes = self._scopes, scopes
        defs, self._definitions = self._definitions, defs
        precomputed_locals, self._precomputed_locals = self._precomputed_locals, precomputed_locals
        yield
        self._definitions = defs
        self._scopes = scopes
        self._scope_depths = scope_depths
        self._precomputed_locals = precomputed_locals

    def process_functions_bodies(self):
        for fnode, defs, scopes, scope_depths, precomputed_locals in self._defered:
            visitor = getattr(self,
                              "visit_{}".format(type(fnode).__name__))
            with self.SwitchScopeContext(defs, scopes, scope_depths, precomputed_locals):
                visitor(fnode, step=DefinitionStep)

    def process_annotations(self):
        compute_defs, self.defs = self.defs,  self.compute_annotation_defs
        for annnode, heads, cb in self._defered_annotations[-1]:
            visitor = getattr(self,
                                "visit_{}".format(type(annnode).__name__))
            currenthead, self._scopes = self._scopes, heads
            cb(visitor(annnode)) if cb else visitor(annnode)
            self._scopes = currenthead
        self.defs = compute_defs

    # stmt
    def visit_Module(self, node):
        self.module = node

        futures = collect_future_imports(node)
        # determine whether the PEP563 is enabled
        # allow manual enabling of DefUseChains.future_annotations
        self.future_annotations |= 'annotations' in futures


        with self.ScopeContext(node):


            self._definitions[-1].update(
                {k: ordered_set((v,)) for k, v in self._builtins.items()}
            )

            self._defered_annotations.append([])
            self.process_body(node.body)

            # handle function bodies
            self.process_functions_bodies()

            # handle defered annotations as in from __future__ import annotations
            self.process_annotations()
            self._defered_annotations.pop()

            # various sanity checks
            if __debug__:
                overloaded_builtins = set()
                for d in self.locals[node]:
                    name = d.name()
                    if name in self._builtins:
                        overloaded_builtins.add(name)
                    assert name in self._definitions[0], (name, d.node)

                nb_defs = len(self._definitions[0])
                nb_bltns = len(self._builtins)
                nb_overloaded_bltns = len(overloaded_builtins)
                nb_heads = len({d.name() for d in self.locals[node]})
                assert nb_defs == nb_heads + nb_bltns - nb_overloaded_bltns

        assert not self._definitions
        assert not self._defered_annotations
        assert not self._scopes
        assert not self._scope_depths
        assert not self._precomputed_locals

    def set_definition(self, name, dnode_or_dnodes, index=-1):
        if self._deadcode:
            return
        
        if isinstance(dnode_or_dnodes, Def):
            dnodes = ordered_set((dnode_or_dnodes,))
        else:
            dnodes = ordered_set(dnode_or_dnodes)

        # set the islive flag to False on killed Defs
        for d in self._definitions[index].get(name, ()):
            if not isinstance(d.node, _ast.AST):
                # A builtin: we never explicitely mark the builtins as killed, since 
                # it can be easily deducted.
                continue
            if d in dnodes or any(d in definitions.get(name, ()) for 
                   definitions in self._definitions[:index]):
                # The definition exists in another definition context, so we can't
                # be sure wether it's killed or not, this happens when:
                # - a variable is conditionnaly declared (d in dnodes)
                # - a variable is conditionnaly killed (any(...))
                continue
            d.islive = False
        
        self._definitions[index][name] = dnodes

    @staticmethod
    def add_to_definition(definition, name, dnode_or_dnodes):
        if isinstance(dnode_or_dnodes, Def):
            definition[name].add(dnode_or_dnodes)
        else:
            definition[name].update(dnode_or_dnodes)

    def extend_definition(self, name, dnode_or_dnodes):
        if self._deadcode:
            return
        DefUseChains.add_to_definition(self._definitions[-1], name,
                                       dnode_or_dnodes)

    def extend_global(self, name, dnode_or_dnodes):
        if self._deadcode:
            return
        # `name` *should* be in self._definitions[0] because we extend the
        # globals. Yet the original code maybe faulty and we need to cope with
        # it.
        if name not in self._definitions[0]:
            if isinstance(dnode_or_dnodes, Def):
                self.locals[self.module].append(dnode_or_dnodes)
            else:
                self.locals[self.module].extend(dnode_or_dnodes)
        DefUseChains.add_to_definition(self._definitions[0], name,
                                       dnode_or_dnodes)

    def set_or_extend_global(self, name, dnode):
        if self._deadcode:
            return
        if name not in self._definitions[0]:
            self.locals[self.module].append(dnode)
        DefUseChains.add_to_definition(self._definitions[0], name, dnode)

    def visit_annotation(self, node):
        annotation = getattr(node, 'annotation', None)
        if annotation:
            self.visit(annotation)

    def visit_skip_annotation(self, node):
        if type(node).__name__ == 'Name':
            self.visit_Name(node, skip_annotation=True)
        else:
            self.visit(node)

    def visit_FunctionDef(self, node, step=DeclarationStep, in_def695=False):
        if step is DeclarationStep:
            dnode = self.chains.setdefault(node, Def(node))
            self.add_to_locals(node.name, dnode)
            currentscopes = list(self._scopes)
            
            if not in_def695:

                for kw_default in filter(None, node.args.kw_defaults):
                    self.visit(kw_default).add_user(dnode)
                for default in node.args.defaults:
                    self.visit(default).add_user(dnode)
                
                if self.is_stub:
                    for decorator in node.decorator_list:
                        self._defered_annotations[-1].append((
                            decorator, currentscopes, None))
                else:
                    for decorator in node.decorator_list:
                        self.visit(decorator)

                if any(getattr(node, 'type_params', [])):
                    self.visit_def695(def695(body=node.type_params, d=node))
                    return
            
            parent_typename = type(currentscopes[-1 if not in_def695 else -2]).__name__

            if not self.future_annotations:
                for arg in _iter_arguments(node.args):
                    annotation = getattr(arg, 'annotation', None)
                    if annotation:
                        if in_def695:
                            try:
                                _validate_annotation_body(annotation)
                                if parent_typename == 'ClassDef':
                                    _validate_annotation_body_within_class_scope(annotation)
                            except SyntaxError as e :
                                self.warn(str(e), annotation)
                                continue
                        self.visit(annotation)

            else:
                # annotations are to be analyzed later as well
                if node.returns:
                    try:
                        _validate_annotation_body(node.returns)
                        if in_def695 and parent_typename == 'ClassDef':
                            _validate_annotation_body_within_class_scope(node.returns)
                    except SyntaxError as e :
                        self.warn(str(e), node.returns)
                    else:
                        self._defered_annotations[-1].append(
                            (node.returns, currentscopes, None))
                for arg in _iter_arguments(node.args):
                    if arg.annotation:
                        try:
                            _validate_annotation_body(arg.annotation)
                            if in_def695 and parent_typename == 'ClassDef':
                                _validate_annotation_body_within_class_scope(arg.annotation)
                        except SyntaxError as e :
                            self.warn(str(e), arg.annotation)
                            continue
                        self._defered_annotations[-1].append(
                            (arg.annotation, currentscopes, None))

            if not self.future_annotations and node.returns:
                if in_def695:
                    try:
                        _validate_annotation_body(node.returns)
                        if in_def695 and parent_typename == 'ClassDef':
                                _validate_annotation_body_within_class_scope(node.returns)
                    except SyntaxError as e:
                        self.warn(str(e), node.returns)
                    else:
                        self.visit(node.returns)
                else:
                    self.visit(node.returns)

            if in_def695:
                # emulate this (except f is not actually defined in both scopes): 
                # def695 __generic_parameters_of_f():
                #     T = TypeVar(name='T')
                #     def f(x: T) -> T:
                #         return x
                #     return f
                # f = __generic_parameters_of_f()
                self.set_definition(node.name, dnode, index=-2)
            else:
                self.set_definition(node.name, dnode)

            self._defered.append((node,
                                  list(self._definitions),
                                  list(self._scopes),
                                  list(self._scope_depths),
                                  list(self._precomputed_locals)))
        
        elif step is DefinitionStep:
            with self.ScopeContext(node):
                for arg in _iter_arguments(node.args):
                    self.visit_skip_annotation(arg)
                self.process_body(node.body)
        else:
            raise NotImplementedError()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node, in_def695=False):
        dnode = self.chains.setdefault(node, Def(node))
        self.add_to_locals(node.name, dnode)
        currentscopes = list(self._scopes)

        if not in_def695:
            if self.is_stub:
                for decorator in node.decorator_list:
                    self._defered_annotations[-1].append((
                        decorator, currentscopes, lambda ddecorator: ddecorator.add_user(dnode)))
            else:
                for decorator in node.decorator_list:
                    self.visit(decorator).add_user(dnode)

            if any(getattr(node, 'type_params', [])):
                self.visit_def695(def695(body=node.type_params, d=node))
                return
        
        parent_typename = type(currentscopes[-1 if not in_def695 else -2]).__name__

        if self.is_stub:
            # special treatment for classes in stub modules
            # so they can contain forward-references.
            for base in node.bases:
                if in_def695:
                    try:
                        _validate_annotation_body(base)
                        if parent_typename == 'ClassDef':
                            _validate_annotation_body_within_class_scope(base)
                    except SyntaxError as e:
                        self.warn(str(e), base)
                        continue
                self._defered_annotations[-1].append((
                    base, currentscopes, lambda dbase: dbase.add_user(dnode)))
            for keyword in node.keywords:
                if in_def695:
                    try:
                        _validate_annotation_body(keyword)
                        if parent_typename == 'ClassDef':
                            _validate_annotation_body_within_class_scope(keyword)
                    except SyntaxError as e:
                        self.warn(str(e), keyword)
                        continue
                self._defered_annotations[-1].append((
                    keyword.value, currentscopes, lambda dkeyword: dkeyword.add_user(dnode)))
            
        else:
            for base in node.bases:
                if in_def695:
                    try:
                        _validate_annotation_body(base)
                        if parent_typename == 'ClassDef':
                            _validate_annotation_body_within_class_scope(base)
                    except SyntaxError as e:
                        self.warn(str(e), base)
                        continue
                self.visit(base).add_user(dnode)
            for keyword in node.keywords:
                if in_def695:
                    try:
                        _validate_annotation_body(keyword)
                        if parent_typename == 'ClassDef':
                            _validate_annotation_body_within_class_scope(keyword)
                    except SyntaxError as e:
                        self.warn(str(e), keyword)
                        continue
                self.visit(keyword.value).add_user(dnode)

        with self.ScopeContext(node):
            self.set_definition("__class__", Def("__class__"))
            self.process_body(node.body)

        if in_def695:
            # see comment in visit_FunctionDef
            self.set_definition(node.name, dnode, index=-2)
        else:
            self.set_definition(node.name, dnode)

    def visit_Return(self, node):
        if node.value:
            self.visit(node.value)

    def visit_Break(self, _):
        for k, v in self._definitions[-1].items():
            DefUseChains.add_to_definition(self._breaks[-1], k, v)
        self._definitions[-1].clear()

    def visit_Continue(self, _):
        for k, v in self._definitions[-1].items():
            DefUseChains.add_to_definition(self._continues[-1], k, v)
        self._definitions[-1].clear()

    def visit_Delete(self, node):
        for target in node.targets:
            self.visit(target)

    def visit_Assign(self, node):
        # link is implicit through ctx
        self.visit(node.value)
        for target in node.targets:
            self.visit(target)
    
    def visit_AnnAssign(self, node):
        if (self.is_stub and node.value and matches_typing_name(
                self._scopes, self.locals, self.imports, self._modnames,
                node.annotation, 'TypeAlias')):
            # support for PEP 613 - Explicit Type Aliases
            # BUT an untyped global expression 'x=int' will NOT be considered a type alias.
            self._defered_annotations[-1].append(
                (node.value, list(self._scopes), None))
        elif node.value:
            dvalue = self.visit(node.value)
        
        if not self.future_annotations:
            self.visit(node.annotation)
        else:
            try:
                _validate_annotation_body(node.annotation)
            except SyntaxError as e:
                self.warn(str(e), node.annotation)
            else:
                self._defered_annotations[-1].append(
                    (node.annotation, list(self._scopes), None))
        self.visit(node.target)

    def visit_AugAssign(self, node):
        dvalue = self.visit(node.value)
        if type(node.target).__name__ == 'Name':
            ctx, node.target.ctx = node.target.ctx, ast.Load()
            dtarget = self.visit(node.target)
            dvalue.add_user(dtarget)
            node.target.ctx = ctx
            if any(node.target.id in _globals for _globals in self._globals):
                self.extend_global(node.target.id, dtarget)
            else:
                loaded_from = [d.name() for d in self.defs(node.target,
                                                           quiet=True)]
                self.set_definition(node.target.id, dtarget)
                # If we augassign from a value that comes from '*', let's use
                # this node as the definition point.
                if '*' in loaded_from:
                    self.locals[self._scopes[-1]].append(dtarget)
        else:
            self.visit(node.target).add_user(dvalue)
    
    def visit_TypeAlias(self, node, in_def695=False):
        # Generic type aliases:
        # type Alias[T: int] = list[T]

        # Equivalent to:
        # def695 __generic_parameters_of_Alias():
        #     def695 __evaluate_T_bound():
        #         return int
        #     T = __make_typevar_with_bound(name='T', evaluate_bound=__evaluate_T_bound)
        #     def695 __evaluate_Alias():
        #         return list[T]
        #     return __make_typealias(name='Alias', type_params=(T,), evaluate_value=__evaluate_Alias)
        # Alias = __generic_parameters_of_Alias()

        if type(node.name).__name__ == 'Name':
            dname = self.chains.setdefault(node.name, Def(node.name))
            self.add_to_locals(node.name.id, dname)

            if not in_def695 and any(getattr(node, 'type_params', [])):
                self.visit_def695(def695(body=node.type_params, d=node))
                return
            
            parent_typename = type(self._scopes[-1 if not in_def695 else -2]).__name__

            dnode = self.chains.setdefault(node, Def(node))
            try:
                _validate_annotation_body(node.value)
                if parent_typename == 'ClassDef':
                    _validate_annotation_body_within_class_scope(node.value)
            except SyntaxError as e:
                self.warn(str(e), node.value)
            else:
                self._defered_annotations[-1].append(
                    (node.value, list(self._scopes), None))

            if in_def695:
                # see comment in visit_FunctionDef
                self.set_definition(node.name.id, dname, index=-2)
            else:
                self.set_definition(node.name.id, dname)
            
            return dnode
        else:
            raise NotImplementedError()

    def visit_For(self, node):
        self.visit(node.iter)

        self._breaks.append(defaultdict(ordered_set))
        self._continues.append(defaultdict(ordered_set))

        self._undefs.append(defaultdict(list))
        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:
            self.visit(node.target)
            self.process_body(node.body)
            self.process_undefs()

            continue_defs = self._continues.pop()
            for d, u in continue_defs.items():
                self.extend_definition(d, u)
            self._continues.append(defaultdict(ordered_set))

            # extra round to ``emulate'' looping
            self.visit(node.target)
            self.process_body(node.body)

            # process else clause in case of late break
            with self.DefinitionContext(defaultdict(ordered_set)) as orelse_defs:
                self.process_body(node.orelse)

            break_defs = self._breaks.pop()
            continue_defs = self._continues.pop()


        for d, u in orelse_defs.items():
            self.extend_definition(d, u)

        for d, u in continue_defs.items():
            self.extend_definition(d, u)

        for d, u in break_defs.items():
            self.extend_definition(d, u)

        for d, u in body_defs.items():
            self.extend_definition(d, u)

    visit_AsyncFor = visit_For

    def visit_While(self, node):

        with self.DefinitionContext(self._definitions[-1].copy()):
            self._undefs.append(defaultdict(list))
            self._breaks.append(defaultdict(ordered_set))
            self._continues.append(defaultdict(ordered_set))

            self.process_body(node.orelse)

        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:

            self.visit(node.test)
            self.process_body(node.body)

            self.process_undefs()

            continue_defs = self._continues.pop()
            for d, u in continue_defs.items():
                self.extend_definition(d, u)
            self._continues.append(defaultdict(ordered_set))

            # extra round to simulate loop
            self.visit(node.test)
            self.process_body(node.body)

            # the false branch of the eval
            self.visit(node.test)

            with self.DefinitionContext(self._definitions[-1].copy()) as orelse_defs:
                self.process_body(node.orelse)

        break_defs = self._breaks.pop()
        continue_defs = self._continues.pop()

        for d, u in continue_defs.items():
            self.extend_definition(d, u)

        for d, u in break_defs.items():
            self.extend_definition(d, u)

        for d, u in orelse_defs.items():
            self.extend_definition(d, u)

        for d, u in body_defs.items():
            self.extend_definition(d, u)

    def visit_If(self, node):
        self.visit(node.test)

        # putting a copy of current level to handle nested conditions
        with self.DefinitionContext(self._definitions[-1].copy()) as body_defs:
            self.process_body(node.body)

        with self.DefinitionContext(self._definitions[-1].copy()) as orelse_defs:
            self.process_body(node.orelse)

        for d in body_defs:
            if d in orelse_defs:
                self.set_definition(d, body_defs[d] + orelse_defs[d])
            else:
                self.extend_definition(d, body_defs[d])

        for d in orelse_defs:
            if d in body_defs:
                pass  # already done in the previous loop
            else:
                self.extend_definition(d, orelse_defs[d])

    def visit_With(self, node):
        for withitem in node.items:
            self.visit(withitem)
        self.process_body(node.body)

    visit_AsyncWith = visit_With

    def visit_Raise(self, node):
        self.generic_visit(node)

    def visit_Try(self, node):
        with self.DefinitionContext(self._definitions[-1].copy()) as failsafe_defs:
            self.process_body(node.body)
            self.process_body(node.orelse)

        # handle the fact that definitions may have fail
        for d in failsafe_defs:
            self.extend_definition(d, failsafe_defs[d])

        for excepthandler in node.handlers:
            with self.DefinitionContext(defaultdict(ordered_set)) as handler_def:
                self.visit(excepthandler)

            for hd in handler_def:
                self.extend_definition(hd, handler_def[hd])

        self.process_body(node.finalbody)
    
    visit_TryStar = visit_Try
    
    def visit_Assert(self, node):
        self.visit(node.test)
        if node.msg:
            self.visit(node.msg)

    def add_to_locals(self, name, dnode, index=-1):
        if any(name in _globals for _globals in self._globals):
            self.set_or_extend_global(name, dnode)
        elif dnode not in self.locals[self._scopes[index]]:
            self.locals[self._scopes[index]].append(dnode)

    def visit_Import(self, node):
        for alias in node.names:
            dalias = self.chains.setdefault(alias, Def(alias))
            base = alias.name.split(".", 1)[0]
            self.set_definition(alias.asname or base, dalias)
            self.add_to_locals(alias.asname or base, dalias)
        self.imports.update(parse_import(node, self.modname, is_package=self.is_package))

    def visit_ImportFrom(self, node):
        for alias in node.names:
            dalias = self.chains.setdefault(alias, Def(alias))
            if alias.name == '*':
                self.extend_definition('*', dalias)
            else:
                self.set_definition(alias.asname or alias.name, dalias)
            self.add_to_locals(alias.asname or alias.name, dalias)
        self.imports.update(parse_import(node, self.modname, is_package=self.is_package))

    def visit_Global(self, node):
        for name in node.names:
            self._globals[-1].add(name)

    def visit_Nonlocal(self, node):
        for name in node.names:
            for i, d in enumerate(reversed(self._definitions)):
                if i == 0:
                    continue
                if name not in d:
                    continue
                else:
                    if isinstance(self._scopes[-i-1], def695):
                        # see https://docs.python.org/3.12/reference/executionmodel.html#annotation-scopes
                        self.warn("names defined in annotation scopes cannot be rebound with nonlocal statements", node)
                        break
                    # this rightfully creates aliasing
                    self.set_definition(name, d[name])
                    break
            else:
                self.unbound_identifier(name, node)

    def visit_Expr(self, node):
        self.generic_visit(node)

    # pattern matching

    def visit_Match(self, node):

        self.visit(node.subject)

        defs = []
        for kase in node.cases:
            if kase.guard:
                self.visit(kase.guard)
            self.visit(kase.pattern)
            
            with self.DefinitionContext(self._definitions[-1].copy()) as case_defs:
                self.process_body(kase.body)
            defs.append(case_defs)
        
        if not defs:
            return
        if len(defs) == 1:
            body_defs, orelse_defs, rest = defs[0], [], []
        else:
            body_defs, orelse_defs, rest = defs[0], defs[1], defs[2:]
        while True:
            # merge defs, like in if-else but repeat the process for x branches       
            for d in body_defs:
                if d in orelse_defs:
                    self.set_definition(d, body_defs[d] + orelse_defs[d])
                else:
                    self.extend_definition(d, body_defs[d])
            for d in orelse_defs:
                if d not in body_defs:
                    self.extend_definition(d, orelse_defs[d])
            if not rest:
                break
            body_defs = self._definitions[-1]
            orelse_defs, rest = rest[0], rest[1:]
    
    def visit_MatchValue(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value)
        return dnode

    visit_MatchSingleton = visit_MatchValue
    
    def visit_MatchSequence(self, node):
        # mimics a list
        with _setattrs(node, ctx=ast.Load(), elts=node.patterns):
            return self.visit_List(node)
    
    def visit_MatchMapping(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        with _setattrs(node, values=node.patterns):
            # mimics a dict
            self.visit_Dict(node)
        if node.rest:
            with _setattrs(node, id=node.rest, ctx=ast.Store(), annotation=None):
                self.visit_Name(node)
        return dnode
    
    def visit_MatchClass(self, node):
        # mimics a call
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.cls).add_user(dnode)
        for arg in node.patterns:
            self.visit(arg).add_user(dnode)
        for kw in node.kwd_patterns:
            self.visit(kw).add_user(dnode)
        return dnode
    
    def visit_MatchStar(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.name:
            # mimics store name
            with _setattrs(node, id=node.name, ctx=ast.Store(), annotation=None):
                self.visit_Name(node)
        return dnode
    
    def visit_MatchAs(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.pattern:
            self.visit(node.pattern)
        if node.name:
            with _setattrs(node, id=node.name, ctx=ast.Store(), annotation=None):
                self.visit_Name(node)
        return dnode
    
    def visit_MatchOr(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for pat in node.patterns:
            self.visit(pat).add_user(dnode)
        return dnode

    # expressions

    def visit_BoolOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    def visit_BinOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.left).add_user(dnode)
        self.visit(node.right).add_user(dnode)
        return dnode

    def visit_UnaryOp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.operand).add_user(dnode)
        return dnode

    def visit_Lambda(self, node, step=DeclarationStep):
        if step is DeclarationStep:
            dnode = self.chains.setdefault(node, Def(node))
            for default in node.args.defaults:
                self.visit(default).add_user(dnode)
            # a lambda never has kw_defaults
            self._defered.append((node,
                                  list(self._definitions),
                                  list(self._scopes),
                                  list(self._scope_depths),
                                  list(self._precomputed_locals)))
            return dnode
        elif step is DefinitionStep:
            dnode = self.chains[node]
            with self.ScopeContext(node):
                for a in _iter_arguments(node.args):
                    self.visit(a)
                self.visit(node.body).add_user(dnode)
            return dnode
        else:
            raise NotImplementedError()

    def visit_IfExp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.test).add_user(dnode)
        self.visit(node.body).add_user(dnode)
        self.visit(node.orelse).add_user(dnode)
        return dnode

    def visit_Dict(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for key in filter(None, node.keys):
            self.visit(key).add_user(dnode)
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    def visit_Set(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for elt in node.elts:
            self.visit(elt).add_user(dnode)
        return dnode

    def visit_ListComp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        try:
            _validate_comprehension(node)
        except SyntaxError as e:
            self.warn(str(e), node)
            return dnode
        with self.CompScopeContext(node):
            for i, comprehension in enumerate(node.generators):
                self.visit_comprehension(comprehension, 
                                         is_nested=i!=0).add_user(dnode)
            self.visit(node.elt).add_user(dnode)

        return dnode

    visit_SetComp = visit_ListComp

    def visit_DictComp(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        try:
            _validate_comprehension(node)
        except SyntaxError as e:
            self.warn(str(e), node)
            return dnode
        with self.CompScopeContext(node):
            for i, comprehension in enumerate(node.generators):
                self.visit_comprehension(comprehension, 
                                         is_nested=i!=0).add_user(dnode)
            self.visit(node.key).add_user(dnode)
            self.visit(node.value).add_user(dnode)

        return dnode

    visit_GeneratorExp = visit_ListComp

    def visit_Await(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        return dnode

    def visit_Yield(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.value:
            self.visit(node.value).add_user(dnode)
        return dnode

    visit_YieldFrom = visit_Await

    def visit_Compare(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.left).add_user(dnode)
        for expr in node.comparators:
            self.visit(expr).add_user(dnode)
        return dnode

    def visit_Call(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.func).add_user(dnode)
        if self.is_stub and matches_typing_name(
                self._scopes, self.locals, self.imports, self._modnames, 
                node.func, 'TypeVar'):
            # In stubs, constraints and bound argument 
            # of TypeVar() can be forward references.
            current_scopes = list(self._scopes)
            for arg in node.args:
                self._defered_annotations[-1].append(
                    (arg, current_scopes,
                    lambda darg:darg.add_user(dnode)))
            for kw in node.keywords:
                self._defered_annotations[-1].append(
                    (kw.value, current_scopes,
                    lambda dkw:dkw.add_user(dnode)))
        else:
            for arg in node.args:
                self.visit(arg).add_user(dnode)
            for kw in node.keywords:
                self.visit(kw.value).add_user(dnode)
        return dnode

    visit_Repr = visit_Await

    def visit_Constant(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        return dnode

    def visit_FormattedValue(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        if node.format_spec:
            self.visit(node.format_spec).add_user(dnode)
        return dnode

    def visit_JoinedStr(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        for value in node.values:
            self.visit(value).add_user(dnode)
        return dnode

    visit_Attribute = visit_Await

    def visit_Subscript(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        self.visit(node.slice).add_user(dnode)
        return dnode

    def visit_Starred(self, node):
        if type(node.ctx).__name__ == 'Store':
            return self.visit(node.value)
        else:
            dnode = self.chains.setdefault(node, Def(node))
            self.visit(node.value).add_user(dnode)
            return dnode

    def visit_NamedExpr(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.value).add_user(dnode)
        if type(node.target).__name__ == 'Name':
            self.visit_Name(node.target, named_expr=True)
        return dnode

    def _first_non_comprehension_scope(self):
        index = -1
        enclosing_scope = self._scopes[index]
        while type(enclosing_scope).__name__ in _Comp:
            index -= 1
            enclosing_scope = self._scopes[index]
        return index, enclosing_scope

    def visit_Name(self, node, skip_annotation=False, named_expr=False):
        ctx_typename = type(node.ctx).__name__
        if ctx_typename in ('Param', 'Store'):
            dnode = self.chains.setdefault(node, Def(node))
            # FIXME: find a smart way to merge the code below with add_to_locals
            if any(node.id in _globals for _globals in self._globals):
                self.set_or_extend_global(node.id, dnode)
            else:
                # special code for warlus target: should be 
                # stored in first non comprehension scope
                index, enclosing_scope = (self._first_non_comprehension_scope() 
                                          if named_expr else (-1, self._scopes[-1]))

                if index < -1 and type(enclosing_scope).__name__ == 'ClassDef':
                    # invalid named expression, not calling set_definition.
                    self.warn('assignment expression within a comprehension '
                              'cannot be used in a class body', node)
                    return dnode

                self.set_definition(node.id, dnode, index)
                if dnode not in self.locals[self._scopes[index]]:
                    self.locals[self._scopes[index]].append(dnode)

            # Name.annotation is a special case because of gast
            if getattr(node, 'annotation', None) is not None and not skip_annotation and not self.future_annotations:
                self.visit(node.annotation)

        elif ctx_typename in ('Load', 'Del'):
            node_in_chains = node in self.chains
            if node_in_chains:
                dnode = self.chains[node]
            else:
                dnode = Def(node)
            for d in self.defs(node):
                d.add_user(dnode)
            if not node_in_chains:
                self.chains[node] = dnode
            # currently ignore the effect of a del
        else:
            raise NotImplementedError()
        return dnode

    def visit_Destructured(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        tmp_store = ast.Store()
        for elt in node.elts:
            elt_typename = type(elt).__name__
            if elt_typename == 'Name':
                tmp_store, elt.ctx = elt.ctx, tmp_store
                self.visit(elt)
                tmp_store, elt.ctx = elt.ctx, tmp_store
            elif elt_typename in ('Subscript', 'Starred', 'Attribute'):
                self.visit(elt)
            elif elt_typename in ('List', 'Tuple'):
                self.visit_Destructured(elt)
        return dnode

    def visit_List(self, node):
        if type(node.ctx).__name__ == 'Load':
            dnode = self.chains.setdefault(node, Def(node))
            for elt in node.elts:
                self.visit(elt).add_user(dnode)
            return dnode
        # unfortunately, destructured node are marked as Load,
        # only the parent List/Tuple is marked as Store
        elif type(node.ctx).__name__ == 'Store':
            return self.visit_Destructured(node)
        else:
            raise NotImplementedError()

    visit_Tuple = visit_List

    # slice

    def visit_Slice(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.lower:
            self.visit(node.lower).add_user(dnode)
        if node.upper:
            self.visit(node.upper).add_user(dnode)
        if node.step:
            self.visit(node.step).add_user(dnode)
        return dnode
    
    # type params

    def visit_def695(self, node):
        # We don't use two steps here because the declaration 
        # step is the same as definition step for def695's
        # 1.type parameters of generic type aliases, 
        # 2.type parameters and annotations of generic functions and
        # 3.type parameters and base class expressions of generic classes
        # the rest is evaluated as defered annotations:
        # 4.the value of generic type aliases
        # 5.the bounds of type variables
        # 6.the constraints of type variables
        
        # introduce the new scope
        dnode = self.chains.setdefault(node.d, Def(node.d))
        
        with self.ScopeContext(node):
            # visit the type params
            for p in node.body:
                try:
                    _validate_annotation_body(p)
                except SyntaxError as e:
                    self.warn(str(e), p)
                else:
                    self.visit(p).add_user(dnode)
            # then visit the actual node while 
            # being in the def695 scope.
            visitor = getattr(self, "visit_{}".format(type(node.d).__name__))
            visitor(node.d, in_def695=True)

    def visit_TypeVar(self, node):
        # these nodes can only be visited under a def695 scope
        dnode = self.chains.setdefault(node, Def(node))
        self.set_definition(node.name, dnode)
        self.add_to_locals(node.name, dnode)

        if type(node).__name__ == 'TypeVar' and node.bound:
            self._defered_annotations[-1].append(
                (node.bound, list(self._scopes), None))
        
        return dnode

    visit_ParamSpec = visit_TypeVarTuple = visit_TypeVar

    # misc

    def visit_comprehension(self, node, is_nested):
        dnode = self.chains.setdefault(node, Def(node))
        if not is_nested:
            # There's one part of a comprehension or generator expression that executes in the surrounding scope, 
            # it's the expression for the outermost iterable.
            with self.SwitchScopeContext(self._definitions[:-1], self._scopes[:-1], 
                                        self._scope_depths[:-1], self._precomputed_locals[:-1]):
                self.visit(node.iter).add_user(dnode)
        else:
            # If a comprehension has multiple for clauses, 
            # the iterables of the inner for clauses are evaluated in the comprehension's scope:
            self.visit(node.iter).add_user(dnode)
        self.visit(node.target)
        for if_ in node.ifs:
            self.visit(if_).add_user(dnode)
        return dnode

    def visit_excepthandler(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        if node.type:
            self.visit(node.type).add_user(dnode)
        if node.name:
            self.visit(node.name).add_user(dnode)
        self.process_body(node.body)
        return dnode

    # visit_arguments is not implemented on purpose

    def visit_withitem(self, node):
        dnode = self.chains.setdefault(node, Def(node))
        self.visit(node.context_expr).add_user(dnode)
        if node.optional_vars:
            self.visit(node.optional_vars)
        return dnode

def _validate_comprehension(node):
    """
    Raises SyntaxError if:
     - a named expression is used in a comprehension iterable expression
     - a named expression rebinds a comprehension iteration variable
    """
    iter_names = set() # comprehension iteration variables
    for gen in node.generators:
        for namedexpr in (n for n in ast.walk(gen.iter) if type(n).__name__ == 'NamedExpr'):
            raise SyntaxError('assignment expression cannot be used '
                                'in a comprehension iterable expression')
        iter_names.update(n.id for n in ast.walk(gen.target) 
            if type(n).__name__ == 'Name' and type(n.ctx).__name__ == 'Store')
    for namedexpr in (n for n in ast.walk(node) if  type(n).__name__ == 'NamedExpr'):
        bound = getattr(namedexpr.target, 'id', None)
        if bound in iter_names:
            raise SyntaxError('assignment expression cannot rebind '
                              "comprehension iteration variable '{}'".format(bound))

_node_type_to_human_name = {
    'NamedExpr': 'assignment expression',
    'Yield': 'yield keyword',
    'YieldFrom': 'yield keyword',
    'Await': 'await keyword',
    'ListComp': 'comprehension',
    'SetComp': 'comprehension',
    'DictComp': 'comprehension',
    'GeneratorExp': 'generator expression',
    'Lambda': 'lambda expression'
}

def _validate_annotation_body(node):
    """
    Raises SyntaxError if:
    - the warlus operator is used
    - the yield/ yield from statement is used
    - the await keyword is used
    """
    for illegal in (n for n in ast.walk(node) if type(n).__name__ in  
                    ('NamedExpr', 'Yield', 'YieldFrom', 'Await')):
        name = _node_type_to_human_name.get(type(illegal).__name__, 'current syntax')
        raise SyntaxError(f'{name} cannot be used in annotation-like scopes')

def _validate_annotation_body_within_class_scope(node):
    """
    Raises SyntaxError if a nested scope is used.
    """
    for illegal in (n for n in ast.walk(node) if type(n).__name__ in  
                    ('ListComp', 'GeneratorExp', 'SetComp', 'DictComp', 'Lambda')):
        name = _node_type_to_human_name.get(type(illegal).__name__, 'current syntax')
        raise SyntaxError(f'{name} cannot be used in annotation scope within class scope')

def _iter_arguments(args):
    """
    Yields all arguments of the given ast.arguments instance.
    """
    for arg in args.args:
        yield arg
    for arg in getattr(args, 'posonlyargs', ()):
        yield arg
    if args.vararg:
        yield args.vararg
    for arg in args.kwonlyargs:
        yield arg
    if args.kwarg:
        yield args.kwarg

def lookup_annotation_name_defs(name, heads, locals_map):
    r"""
    Simple identifier -> defs resolving.

    Lookup a name with the provided head nodes using the locals_map.
    Note that nonlocal and global keywords are ignored by this function.
    Only used to resolve annotations when PEP 563 is enabled.

    :param name: The identifier we're looking up.
    :param heads: List of ast scope statement that describe
        the path to the name context. i.e ``[<Module>, <ClassDef>, <FunctionDef>]``.
        The lookup will happend in the context of the body of tail of ``heads``
        Can be gathered with `Ancestors.parents`.
    :param locals_map: `DefUseChains.locals`.

    :raise LookupError: For
        - builtin names
        - wildcard imported names
        - unbound names

    :raise ValueError: When the heads is empty.

    This function can be used by client code like this:

    >>> import gast as ast
    >>> module = ast.parse("from b import c;import typing as t\nclass C:\n def f(self):self.var = c.Thing()")
    >>> duc = DefUseChains()
    >>> duc.visit(module)
    >>> ancestors = Ancestors()
    >>> ancestors.visit(module)
    ... # we're placing ourselves in the context of the function body
    >>> fn_scope = module.body[-1].body[-1]
    >>> assert isinstance(fn_scope, ast.FunctionDef)
    >>> heads = ancestors.parents(fn_scope) + [fn_scope]
    >>> print(lookup_annotation_name_defs('t', heads, duc.locals)[0])
    t -> ()
    >>> print(lookup_annotation_name_defs('c', heads, duc.locals)[0])
    c -> (c -> (Attribute -> (Call -> ())))
    >>> print(lookup_annotation_name_defs('C', heads, duc.locals)[0])
    C -> ()
    """
    scopes = _get_lookup_scopes(heads)
    scopes_len = len(scopes)
    if scopes_len > 1 and not isinstance(scopes[-1], def695):
        # start by looking at module scope first,
        # then try the theoretical runtime scopes.
        # putting the global scope last in the list so annotation are
        # resolve using he global namespace first. this is the way pyright does.
        # EXCEPT is we're direcly inside a pep695 scope, in this case follow usual rules.
        scopes.append(scopes.pop(0))
    try:
        return _lookup(name, scopes, locals_map)
    except LookupError:
        if name in BuiltinsSrc:
            raise LookupError(f'{name} is a builtin')
        try:
            _lookup(name, scopes, locals_map, only_live=False)
        except LookupError:
            defined_names = [d.name() for s in scopes for d in locals_map[s]]
            raise LookupError("'{}' not found in scopes: {} (heads={}) (available names={})".format(name, scopes, heads, defined_names))
        else:
            raise LookupError("'{}' is killed".format(name))

def _get_lookup_scopes(heads):
    # heads[-1] is the direct enclosing scope and heads[0] is the module.
    # returns a list based on the elements of heads, but with
    # the ignorable scopes removed. Ignorable in the sens that the lookup
    # will never happend in this scope for the given context.

    heads = list(heads) # avoid modifying the list (important)
    try:
        direct_scopes = [heads.pop(-1)] # this scope is the only one that can be a class, expect in case of the presence of def695
    except IndexError:
        raise ValueError('invalid heads: must include at least one element')
    try:
        global_scope = heads.pop(0)
    except IndexError:
        # we got only a global scope
        return direct_scopes
    else:
        if (heads and isinstance(direct_scopes[-1], def695) and 
            type(heads[-1]).__name__ == 'ClassDef'):
            # include the enclosing class scope in case we're in a def695 scope
            direct_scopes.insert(0, heads.pop(-1))
    # more of less modeling what's described here.
    # https://github.com/gvanrossum/gvanrossum.github.io/blob/main/formal/scopesblog.md
    other_scopes = [s for s in heads if type(s).__name__ in _ClosedScopes]
    return [global_scope] + other_scopes + direct_scopes

def _lookup(name, scopes, locals_map, only_live=True):
    context = scopes[-1]
    defs = []
    for loc in locals_map.get(context, ()):
        if loc.name() == name and (loc.islive if only_live else True):
            defs.append(loc)
    if defs:
        return defs
    elif len(scopes)==1:
        raise LookupError()
    return _lookup(name, scopes[:-1], locals_map)

class UseDefChains(object):
    """
    DefUseChains adaptor that builds a mapping between each user
    and the Def that defines this user:
        - chains: Dict[node, List[Def]], a mapping between nodes and the Defs
          that define it.
    """

    def __init__(self, defuses):
        self.chains = {}
        for chain in defuses.chains.values():
            if type(chain.node).__name__ == 'Name':
                self.chains.setdefault(chain.node, [])
            for use in chain.users():
                self.chains.setdefault(use.node, []).append(chain)

        for chain in defuses._builtins.values():
            for use in chain.users():
                self.chains.setdefault(use.node, []).append(chain)

    def __str__(self):
        out = []
        for k, uses in self.chains.items():
            kname = Def(k).name()
            kstr = "{} <- {{{}}}".format(
                kname, ", ".join(sorted(use.name() for use in uses))
            )
            out.append((kname, kstr))
        out.sort()
        return ", ".join(s for k, s in out)
