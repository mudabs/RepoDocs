# Cross-repository candidate fixture

The frontend declares an axios base URL and literal/template requests. Two
backends expose duplicate method/path pairs at conflicting origins. Relative
requests keep competing candidates; absolute URLs reject conflicting declared
origins. DELETE has no backend match. One source URL deliberately contains
credential/query/fragment sentinels that must never enter persisted IR or docs.

OpenAPI 3.1.0 and 3.0.3 JSON inputs exercise paths, operations, literal servers,
and a same-document JSON Pointer Path Item reference.
