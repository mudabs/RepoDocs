"""Structural Java scanning and configuration key readers used by framework adapters."""

from __future__ import annotations

from repodocs.analyzers.config_files import read_properties_keys, read_yaml_keys
from repodocs.analyzers.java import annotation_values, generic_arguments, mask_comments, parse_java, strip_generics


SOURCE = '''package com.example;

import java.util.List;

/** Doc comment with @RestController that must be ignored. */
// @Service in a line comment must be ignored too
@RestController
@RequestMapping(path = "/api", produces = {"application/json"})
public class Api {

    private static final String DYNAMIC = "/dyn";
    private final Service service; // trailing comment

    public Api(Service service, @Qualifier("x") Other other) {
        String local = "{ not a member; }";
        this.service = service;
    }

    @GetMapping("/{id}")
    public ResponseEntity<Item> get(@PathVariable("id") Long id) throws Exception {
        return null;
    }

    public <T> T generic(Map<String, T> input) { return null; }

    public enum Status { OPEN, CLOSED; public String label() { return name(); } }

    @Entity
    @Table(name = "items", indexes = {@Index(columnList = "status")})
    static class Item {
        @Id private Long id;
        @OneToMany(mappedBy = "item") private List<Line> lines = new ArrayList<>();
    }
}

interface ItemRepository extends JpaRepository<Item, Long>, Custom<Item> {
    List<Item> findByStatus(String status);
}

record Point(int x, @Column(name = "yy") int y) implements Serializable {}
'''


def test_parse_java_recovers_types_members_and_lines() -> None:
    unit = parse_java("Api.java", SOURCE)
    assert unit.package == "com.example"
    assert unit.imports == ["java.util.List"]
    by_name = {decl.name: decl for decl in unit.types}
    assert sorted(by_name) == ["Api", "Item", "ItemRepository", "Point", "Status"]

    api = by_name["Api"]
    assert [annotation.simple_name for annotation in api.annotations] == ["RestController", "RequestMapping"]
    assert api.first_line == 7 and api.line == 9
    assert [(field.type, field.name, field.static) for field in api.fields] == [
        ("String", "DYNAMIC", True),
        ("Service", "service", False),
    ]
    methods = {method.name: method for method in api.methods}
    assert methods["Api"].is_constructor
    assert [(p.type, p.name) for p in methods["Api"].parameters] == [("Service", "service"), ("Other", "other")]
    assert [a.simple_name for a in methods["Api"].parameters[1].annotations] == ["Qualifier"]
    assert methods["get"].return_type == "ResponseEntity<Item>"
    assert methods["get"].first_line == 19 and methods["get"].line == 20
    assert methods["get"].annotations[0].args == '"/{id}"'
    assert methods["generic"].return_type == "T"
    assert methods["generic"].parameters[0].type == "Map<String,T>"

    item = by_name["Item"]
    assert item.outer == "Api"
    assert item.has_annotation("Entity") and item.annotation("Table").args.startswith("name = ")
    assert [(field.name, [a.simple_name for a in field.annotations]) for field in item.fields] == [
        ("id", ["Id"]),
        ("lines", ["OneToMany"]),
    ]
    assert by_name["Status"].kind == "enum" and by_name["Status"].fields == []

    repository = by_name["ItemRepository"]
    assert repository.kind == "interface"
    assert repository.extends == ["JpaRepository<Item,Long>", "Custom<Item>"]
    assert [method.name for method in repository.methods] == ["findByStatus"]

    point = by_name["Point"]
    assert point.kind == "record" and point.implements == ["Serializable"]
    assert [(field.type, field.name) for field in point.fields] == [("int", "x"), ("int", "y")]
    assert point.fields[1].annotation("Column") is not None


def test_mask_comments_preserves_offsets_and_strings() -> None:
    source = 'a = "// not a comment"; // real\n/* block\n comment */ b'
    masked = mask_comments(source)
    assert len(masked) == len(source)
    assert masked.count("\n") == source.count("\n")
    assert '"// not a comment"' in masked
    assert "real" not in masked and "block" not in masked
    assert masked.endswith(" b")


def test_annotation_values_and_type_helpers() -> None:
    values = annotation_values('path = "/a", value = {"/b", "/c"}, method = RequestMethod.GET')
    assert values["path"].literals == ("/a",) and values["path"].is_literal
    assert values["value"].literals == ("/b", "/c")
    assert not values["method"].is_literal and values["method"].raw == "RequestMethod.GET"
    assert annotation_values('"/only"')["value"].literals == ("/only",)
    assert not annotation_values("CONSTANT")["value"].is_literal
    assert not annotation_values('BASE + "/x"')["value"].is_literal
    assert annotation_values("") == {}
    assert strip_generics("java.util.List<Order>") == "List"
    assert strip_generics("Order[]") == "Order"
    assert generic_arguments("Map<String, List<Order>>") == ["String", "List<Order>"]


def test_yaml_keys_are_flattened_and_values_dropped() -> None:
    result = read_yaml_keys(
        "spring:\n  datasource:\n    url: jdbc:x\n    password: secret # c\n  list:\n    - a\n    - b\n"
        "---\nspring:\n  profiles: &anchor x\nflow: {a: 1}\ntext: |\n  multi\n  line\nlast: 1\n"
    )
    assert result.keys == [
        "flow",
        "last",
        "spring.datasource.password",
        "spring.datasource.url",
        "spring.list",
        "spring.profiles",
        "text",
    ]
    assert result.notes == [(10, "anchor, alias, or tag value"), (11, "flow collection value")]
    assert "secret" not in " ".join(result.keys)


def test_properties_keys_handle_separators_and_continuations() -> None:
    result = read_properties_keys(
        "# comment\n! bang comment\na.b=1\nc.d : two\ne\\ f value\ng.h = x,\\\n  y\n=novalue\n"
    )
    assert result.keys == ["a.b", "c.d", "e f", "g.h"]
    assert result.notes == [(8, "property line without a key")]
