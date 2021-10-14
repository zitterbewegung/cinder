# Copyright (c) Facebook, Inc. and its affiliates. (http://www.facebook.com)
from __future__ import annotations

import ast
from ast import (
    AST,
    AsyncFunctionDef,
    ClassDef,
    FunctionDef,
    Subscript,
    Index,
    Name,
    NameConstant,
)
from contextlib import nullcontext
from enum import Enum
from functools import partial
from typing import (
    Callable as typingCallable,
    ContextManager,
    Dict,
    List,
    Optional,
    Set,
    TYPE_CHECKING,
    Tuple,
    Union,
)

from ..symbols import Scope, ModuleScope
from .errors import TypedSyntaxError
from .types import (
    Callable,
    CType,
    Callable,
    Class,
    ClassVar,
    DecoratedMethod,
    DynamicClass,
    Function,
    FunctionGroup,
    DYNAMIC_TYPE,
    FLOAT_TYPE,
    FinalClass,
    INT_TYPE,
    MethodType,
    NONE_TYPE,
    Object,
    OPTIONAL_TYPE,
    UNION_TYPE,
    UnionType,
    UnknownDecoratedMethod,
    Value,
)

if TYPE_CHECKING:
    from .compiler import Compiler


class ModuleFlag(Enum):
    CHECKED_DICTS = 1
    SHADOW_FRAME = 2
    CHECKED_LISTS = 3


class ModuleTable:
    def __init__(
        self,
        name: str,
        filename: str,
        compiler: Compiler,
        members: Optional[Dict[str, Value]] = None,
    ) -> None:
        self.name = name
        self.filename = filename
        self.children: Dict[str, Value] = members or {}
        self.compiler = compiler
        self.types: Dict[AST, Value] = {}
        self.node_data: Dict[Tuple[AST, object], object] = {}
        self.flags: Set[ModuleFlag] = set()
        self.decls: List[Tuple[AST, Optional[str], Optional[Value]]] = []
        # TODO: final constants should be typed to literals, and
        # this should be removed in the future
        self.named_finals: Dict[str, ast.Constant] = {}
        # Have we completed our first pass through the module, populating
        # imports and types defined in the module? Until we have, resolving
        # type annotations is not safe.
        self.first_pass_done = False

    def syntax_error(self, msg: str, node: AST) -> None:
        return self.compiler.error_sink.syntax_error(msg, self.filename, node)

    def error_context(self, node: Optional[AST]) -> ContextManager[None]:
        if node is None:
            return nullcontext()
        return self.compiler.error_sink.error_context(self.filename, node)

    def declare_class(self, node: ClassDef, klass: Class) -> None:
        self.decls.append((node, node.name, klass))
        self.children[node.name] = klass

    def declare_function(self, func: Function) -> None:
        existing = self.children.get(func.func_name)
        new_member = func
        if existing is not None:
            if isinstance(existing, Function):
                new_member = FunctionGroup([existing, new_member])
            elif isinstance(existing, FunctionGroup):
                existing.functions.append(new_member)
                new_member = existing
            else:
                raise TypedSyntaxError(
                    f"function conflicts with other member {func.func_name} in {self.name}"
                )

        self.decls.append((func.node, func.func_name, new_member))
        self.children[func.func_name] = new_member

    def _get_inferred_type(self, value: ast.expr) -> Optional[Value]:
        if not isinstance(value, ast.Name):
            return None
        return self.children.get(value.id)

    def finish_bind(self) -> None:
        self.first_pass_done = True
        for node, name, value in self.decls:
            with self.error_context(node):
                if value is not None:
                    assert name is not None
                    new_value = value.finish_bind(self)
                    if new_value is None:
                        del self.children[name]
                    elif new_value is not value:
                        self.children[name] = new_value

                if isinstance(node, ast.AnnAssign):
                    typ = self.resolve_annotation(node.annotation, is_declaration=True)
                    if typ is not None:
                        # Special case Final[dynamic] to use inferred type.
                        target = node.target
                        instance = typ.instance
                        value = node.value
                        if (
                            value is not None
                            and isinstance(typ, FinalClass)
                            and isinstance(typ.unwrap(), DynamicClass)
                        ):
                            instance = self._get_inferred_type(value) or instance

                        if isinstance(target, ast.Name):
                            self.children[target.id] = instance

                    if isinstance(typ, FinalClass):
                        target = node.target
                        value = node.value
                        if not value:
                            raise TypedSyntaxError(
                                "Must assign a value when declaring a Final"
                            )
                        elif (
                            not isinstance(typ, CType)
                            and isinstance(target, ast.Name)
                            and isinstance(value, ast.Constant)
                        ):
                            self.named_finals[target.id] = value

        # We don't need these anymore...

        self.decls.clear()

    def finish_decorator(
        self, node: FunctionDef | AsyncFunctionDef, func: Function
    ) -> Optional[Value]:
        res: Optional[Value] = func
        for decorator in reversed(node.decorator_list):
            decorator_type = self.resolve_decorator(decorator) or DYNAMIC_TYPE
            res = decorator_type.bind_decorate_function(res, decorator)
            if res is None:
                self.types[node] = UnknownDecoratedMethod(func)
                return None

        self.types[node] = res
        return res

    def resolve_type(self, node: ast.AST) -> Optional[Class]:
        # TODO handle Call
        typ = self._resolve(node, self.resolve_type)
        if isinstance(typ, Class):
            return typ

    def resolve_decorator(self, node: ast.AST) -> Optional[Value]:
        if isinstance(node, ast.Call):
            func = self.resolve_decorator(node.func)
            if isinstance(func, Class):
                return func.instance
            elif isinstance(func, Callable):
                return func.return_type.resolved().instance
            elif isinstance(func, MethodType):
                return func.function.return_type.resolved().instance

        return self._resolve(node, self.resolve_decorator)

    def _resolve(
        self,
        node: ast.AST,
        _resolve: typingCallable[[ast.AST], Optional[Value]],
        _resolve_subscr_target: Optional[
            typingCallable[[ast.AST], Optional[Class]]
        ] = None,
    ) -> Optional[Value]:
        if isinstance(node, ast.Name):
            return self.resolve_name(node.id)
        elif isinstance(node, Subscript):
            slice = node.slice
            if isinstance(slice, Index):
                val = (_resolve_subscr_target or _resolve)(node.value)
                if val is not None:
                    value = slice.value
                    if isinstance(value, ast.Tuple):
                        anns = []
                        for elt in value.elts:
                            ann = _resolve(elt) or DYNAMIC_TYPE
                            anns.append(ann)
                        values = tuple(anns)
                        gen = val.make_generic_type(values, self.compiler.generic_types)
                        return gen or val
                    else:
                        index = _resolve(value) or DYNAMIC_TYPE
                        if not isinstance(index, Class):
                            return None
                        gen = val.make_generic_type(
                            (index,), self.compiler.generic_types
                        )
                        return gen or val
        elif isinstance(node, ast.Attribute):
            val = (_resolve_subscr_target or _resolve)(node.value)
            if val is not None:
                return val.resolve_attr(node)

    def resolve_annotation(
        self,
        node: ast.AST,
        *,
        is_declaration: bool = False,
    ) -> Optional[Class]:
        assert self.first_pass_done, (
            "Type annotations cannot be resolved until after initial pass, "
            "so that all imports and types are available."
        )

        with self.error_context(node):
            klass = self._resolve_annotation(node)

            if not is_declaration:
                if isinstance(klass, FinalClass):
                    raise TypedSyntaxError(
                        "Final annotation is only valid in initial declaration "
                        "of attribute or module-level constant",
                    )
                if isinstance(klass, ClassVar):
                    raise TypedSyntaxError(
                        "ClassVar is allowed only in class attribute annotations. "
                        "Class Finals are inferred ClassVar; do not nest with Final."
                    )

            # Even if we know that e.g. `builtins.str` is the exact `str` type and
            # not a subclass, and it's useful to track that knowledge, when we
            # annotate `x: str` that annotation should not exclude subclasses.
            if klass:
                klass = klass.inexact_type()
                # PEP-484 specifies that ints should be treated as a subclass of floats,
                # even though they differ in the runtime. We need to maintain the distinction
                # between the two internally, so we should view user-specified `float` annotations
                # as `float | int`. This widening of the type prevents us from applying
                # optimizations # to user-specified floats, but does not affect ints. Since we
                # don't optimize Python floats anyway, we accept this to maintain PEP-484 compatibility.

                if klass is FLOAT_TYPE:
                    klass = UNION_TYPE.make_generic_type(
                        (FLOAT_TYPE, INT_TYPE), self.compiler.generic_types
                    )

            # TODO until we support runtime checking of unions, we must for
            # safety resolve union annotations to dynamic (except for
            # optionals, which we can check at runtime)
            if (
                isinstance(klass, UnionType)
                and klass is not UNION_TYPE
                and klass is not OPTIONAL_TYPE
                and klass.opt_type is None
            ):
                return None

            return klass

    def _resolve_annotation(self, node: ast.AST) -> Optional[Class]:
        # First try to resolve non-annotation-specific forms. For resolving the
        # outer target of a subscript (e.g. `Final` in `Final[int]`) we pass
        # `is_declaration=True` to allow `Final` in that position; if in fact
        # we are not resolving a declaration, the outer `resolve_annotation`
        # (our caller) will still catch the generic Final that we end up
        # returning.
        typ = self._resolve(
            node,
            self.resolve_annotation,
            _resolve_subscr_target=partial(
                self.resolve_annotation, is_declaration=True
            ),
        )
        if isinstance(typ, Class):
            return typ
        elif isinstance(node, ast.Str):
            # pyre-ignore[16]: `AST` has no attribute `body`.
            return self.resolve_annotation(ast.parse(node.s, "", "eval").body)
        elif isinstance(node, ast.Constant):
            sval = node.value
            if sval is None:
                return NONE_TYPE
            elif isinstance(sval, str):
                return self.resolve_annotation(ast.parse(node.value, "", "eval").body)
        elif isinstance(node, NameConstant) and node.value is None:
            return NONE_TYPE
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            ltype = self.resolve_annotation(node.left)
            rtype = self.resolve_annotation(node.right)
            if ltype is None or rtype is None:
                return None
            return UNION_TYPE.make_generic_type(
                (ltype, rtype), self.compiler.generic_types
            )

    def resolve_name(self, name: str) -> Optional[Value]:
        return self.children.get(name) or self.compiler.builtins.children.get(name)

    def get_final_literal(self, node: AST, scope: Scope) -> Optional[ast.Constant]:
        if not isinstance(node, Name):
            return None

        final_val = self.named_finals.get(node.id, None)
        if (
            final_val is not None
            and isinstance(node.ctx, ast.Load)
            and (
                # Ensure the name is not shadowed in the local scope
                isinstance(scope, ModuleScope)
                or node.id not in scope.defs
            )
        ):
            return final_val

    def declare_variable(self, node: ast.AnnAssign, module: ModuleTable) -> None:
        self.decls.append((node, None, None))

    def declare_variables(self, node: ast.Assign, module: ModuleTable) -> None:
        pass
