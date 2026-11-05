from types import MappingProxyType

import pytest

from qorqut_operations.application import (CatalogContribution, CatalogEntry, OperationDescriptor, ReviewScopeException,
    compose_catalog)
from qorqut_operations.storage import (Assembly, AssemblyBuilder, AssemblyError, FileEffectContribution,
    RecordEffectContribution, StorageContribution, TableSpec, TransactionContribution)
from support import Demo, finalizer


def test_minimal_trusted_composition_freezes_with_explicit_target_identity():
    demo = Demo()
    assembly = demo.assembly()
    assert assembly.target.name == "synthetic" and assembly.target.version == 1
    assert len(assembly.target.digest) == 64
    assert {"principals", "signoffs", "apply_receipts", "items"} <= set(assembly.tables)
    assert assembly.owners == ("operations", "demo")
    # The same composition always has the same identity; a different version or schema never does.
    assert Demo().assembly().target.digest == assembly.target.digest
    assert Demo().assembly(version=2).target.digest != assembly.target.digest
    changed = (AssemblyBuilder(target_name="synthetic", target_version=1)
        .add(StorageContribution("demo", {"items": TableSpec(("id",))}, schema=("CREATE TABLE items(id TEXT PRIMARY KEY)",)))
        .add(FileEffectContribution("demo", receipt_history=demo.history))
        .add(RecordEffectContribution("demo", demo.hooks)).freeze())
    assert changed.target.digest != assembly.target.digest


def test_missing_required_hooks_fail_explicitly_at_freeze():
    demo = Demo()
    with pytest.raises(AssemblyError, match="receipt history"):
        AssemblyBuilder(target_name="t", target_version=1).add(RecordEffectContribution("demo", demo.hooks)).freeze()
    with pytest.raises(AssemblyError, match="record effect hooks"):
        AssemblyBuilder(target_name="t", target_version=1).add(
            FileEffectContribution("demo", receipt_history=demo.history)).freeze()
    # A declared required resource lifecycle is a required hook too, and it is refused before any store exists.
    with pytest.raises(AssemblyError, match="resource lifecycle for documents"):
        demo.builder(with_lifecycle=False).freeze()
    with pytest.raises(AssemblyError, match="resource lifecycle for documents"):
        demo.builder(with_lifecycle=False).add(FileEffectContribution("other", {"items": demo.lifecycle})).freeze()
    # Lifecycles alone are not a history.
    with pytest.raises(AssemblyError, match="receipt history"):
        (AssemblyBuilder(target_name="t", target_version=1).add(FileEffectContribution("demo", {"documents": demo.lifecycle}))
            .add(RecordEffectContribution("demo", demo.hooks)).freeze())


def test_registration_is_closed_once_frozen_and_an_assembly_cannot_be_forged():
    demo = Demo()
    builder = demo.builder()
    assembly = builder.freeze()
    assert builder.frozen
    with pytest.raises(AssemblyError, match="frozen"):
        builder.add(StorageContribution("late", {"late": TableSpec(("id",))}))
    with pytest.raises(AssemblyError, match="already frozen"):
        builder.freeze()
    with pytest.raises(AssemblyError, match="AssemblyBuilder.freeze"):
        Assembly(assembly.target, (), {}, {}, (), (), (), None, {}, frozenset(), {}, demo.history, demo.hooks)
    assert isinstance(assembly.tables, MappingProxyType)
    with pytest.raises(TypeError):
        assembly.tables["extra"] = TableSpec(("id",))
    with pytest.raises(Exception):
        assembly.target.name = "other"


@pytest.mark.parametrize("value", [{"owner": "x", "tables": {}}, '{"owner": "x"}', "StorageContribution", lambda: None,
                                   object(), None, ("demo", {})])
def test_caller_supplied_data_cannot_register(value):
    with pytest.raises(AssemblyError, match="typed contributions"):
        AssemblyBuilder(target_name="t", target_version=1).add(value)


def test_subclassed_contribution_is_not_trusted():
    class Sneaky(StorageContribution):
        pass
    with pytest.raises(AssemblyError, match="typed contributions"):
        AssemblyBuilder(target_name="t", target_version=1).add(Sneaky("sneaky", {"x": TableSpec(("id",))}))


def test_duplicates_and_core_redefinition_fail():
    demo = Demo()
    builder = demo.builder()
    with pytest.raises(AssemblyError, match="already registered for owner demo"):
        builder.add(StorageContribution("demo", {"other": TableSpec(("id",))}))
    with pytest.raises(AssemblyError, match="already owned"):
        builder.add(StorageContribution("evil", {"principals": TableSpec(("id",))}))
    with pytest.raises(AssemblyError, match="already owned"):
        builder.add(StorageContribution("evil", {"items": TableSpec(("id",))}))
    with pytest.raises(AssemblyError, match="Only one change publisher"):
        builder.add(TransactionContribution("other", change_publisher=demo.publisher))
    with pytest.raises(AssemblyError, match="finalizer can be registered only once"):
        builder.add(TransactionContribution("other", (finalizer,)))
    with pytest.raises(AssemblyError, match="Lifecycle already registered"):
        builder.add(FileEffectContribution("other", {"documents": demo.lifecycle}))
    with pytest.raises(AssemblyError, match="Only one receipt history"):
        builder.add(FileEffectContribution("third", receipt_history=demo.history))
    with pytest.raises(AssemblyError, match="Only one set of record effect hooks"):
        builder.add(RecordEffectContribution("other", demo.hooks))
    with pytest.raises(AssemblyError, match="finalizer can be registered only once"):
        TransactionContribution("again", (finalizer, finalizer))


def test_contribution_shapes_are_validated():
    with pytest.raises(AssemblyError, match="owner"):
        StorageContribution("Not A Name")
    with pytest.raises(AssemblyError, match="plain name"):
        StorageContribution("demo", {"bad name": TableSpec(("id",))})
    with pytest.raises(AssemblyError, match="plain name"):
        StorageContribution("demo", {"items": {"keys": ("id",)}})
    for reserved in ("change_sequences", "target_identity"):
        with pytest.raises(AssemblyError, match="reserved by the engine"):
            StorageContribution("demo", {reserved: TableSpec(("name",))})
    with pytest.raises(AssemblyError, match="resource type names"):
        StorageContribution("demo", required_lifecycles="documents")
    with pytest.raises(AssemblyError, match="own contribution"):
        StorageContribution("demo", {"items": TableSpec(("id",))}, immutable_fields={"principals": frozenset({"id"})})
    with pytest.raises(AssemblyError, match="non-empty SQL"):
        StorageContribution("demo", schema=("",))
    with pytest.raises(AssemblyError, match="callable"):
        TransactionContribution("demo", ("not callable",))
    with pytest.raises(AssemblyError, match="capture, publish_commit"):
        TransactionContribution("demo", change_publisher=object())
    with pytest.raises(AssemblyError, match="applying, conflicted, applied, released"):
        FileEffectContribution("demo", {"documents": object()})
    with pytest.raises(AssemblyError, match="capture_receipt"):
        FileEffectContribution("demo", receipt_history=object())
    with pytest.raises(AssemblyError, match="tracked tables"):
        class NoTables:
            def apply_effect(self, *a): ...
            def force_revision(self, *a): ...
            def record_applied(self, *a, **k): ...
        RecordEffectContribution("demo", NoTables())
    with pytest.raises(AssemblyError, match="explicit"):
        AssemblyBuilder(target_name="", target_version=1)
    with pytest.raises(AssemblyError, match="explicit positive version"):
        AssemblyBuilder(target_name="t", target_version=0)


def test_catalog_composition_freezes_registry_and_refuses_untyped_or_duplicate_input():
    entry = CatalogEntry("items_list", False, "read")
    descriptor = OperationDescriptor("items_list", {"type": "object"}, effect_kind="read", supports_signoff=False,
                                     read=lambda *_: {})
    composition = compose_catalog([CatalogContribution("demo", (entry,), (descriptor,))])
    registry = composition.registry
    assert registry.frozen and registry.known("items_list") and registry.known("operations_batch")
    with pytest.raises(ValueError, match="frozen"):
        registry.declare(CatalogEntry("late_entry", False, "read"))
    with pytest.raises(ValueError, match="frozen"):
        registry.register(OperationDescriptor("late_op", {"type": "object"}, effect_kind="read",
                                              supports_signoff=False, read=lambda *_: {}))
    for bad in ({"owner": "demo"}, "demo", object()):
        with pytest.raises(AssemblyError, match="typed catalog contributions"):
            compose_catalog([bad])
    with pytest.raises(AssemblyError, match="already registered for owner"):
        compose_catalog([CatalogContribution("demo", (entry,)), CatalogContribution("demo")])
    with pytest.raises(AssemblyError, match="more than one contribution"):
        compose_catalog([CatalogContribution("one", (entry,)), CatalogContribution("two", (entry,))])
    with pytest.raises(AssemblyError, match="typed CatalogEntry"):
        CatalogContribution("demo", ({"name": "items_list"},))
    with pytest.raises(ValueError, match="already installed"):
        compose_catalog([CatalogContribution("one", (entry,), (descriptor,)), CatalogContribution("two", (), (descriptor,))])
    # An installed handler cannot change the authority metadata the catalog declared for it.
    with pytest.raises(ValueError, match="changes its authority metadata"):
        compose_catalog([CatalogContribution("one", (CatalogEntry("items_create", True, "records"),), (
            OperationDescriptor("items_create", {"type": "object"}, supports_signoff=False,
                                prepare=lambda *_: None, apply=lambda *_: None),))])
    exception = ReviewScopeException(frozenset({"items_create"}), frozenset({"items"}))
    assert compose_catalog([CatalogContribution("demo", review_exceptions=(exception,))]).review_exceptions == (exception,)
    with pytest.raises(AssemblyError, match="typed ReviewScopeException"):
        CatalogContribution("demo", review_exceptions=({"operations": ["items_create"]},))
