# ADR-016: React Adapter Vocabulary: Modules, Components, Routes, and Request Sites

- Date: 2026-11-15
- Status: Accepted

## Context

Issue #15 asks for a React adapter that extracts components, routes, API clients, and imports with evidence, without treating arbitrary function calls as runtime relationships, and that reports dynamic imports and unsupported runtime behaviour. ADR-015 provides the analyzer contract, span evidence, and limitations, but the System IR had no vocabulary for front-end facts, and the outgoing HTTP calls of a front end are the main input the later cross-repository correlation (#17) will match against backend endpoints.

## Alternatives

- Model only components and leave routes and HTTP calls to the AI stage.
- Record every function call as a `calls` edge and let consumers filter.
- Record request sites with literal URLs as nodes of their own, with template holes preserved, and keep every other call out of the IR.
- Resolve URL templates by guessing environment values or base URLs.

## Decision

- `module` nodes represent source files (`.js`, `.jsx`, `.ts`, `.tsx`, `.mjs`, `.cjs`) outside test, story, mock, and build-configuration files. `imports` edges connect a module to another module when a relative specifier resolves to a discovered file, or to the `dependency` node of a declared package. Each edge records the specifier and whether it is `static`, `dynamic` (`import()`), `require`, or a re-export. Specifiers that resolve to neither are reported as `ambiguous-reference` limitations; path aliases are not applied.
- `component` nodes are top-level PascalCase function, arrow, `memo`, `forwardRef`, or class declarations whose statement contains JSX or `createElement`. `references` edges with kind `jsx-element` connect a component to another component defined in the same repository when a JSX element name resolves through the module's imports. No edge is created for function calls, hooks, props, or elements from packages.
- `route` nodes carry the full literal path composed from nested React Router `<Route>` elements or route objects, the declared path, the index flag, the rendering component when it is a literal element or identifier, and the router kind. Next.js `pages/` and `app/**/page` files are routes of kind `next-pages` and `next-app` because the file path is literal evidence; `pages/api/` and `app/**/route` files are `endpoint` nodes exposed by their module. Non-literal paths are `dynamic-routing` limitations.
- `http_request` nodes represent request sites: `fetch`, `axios`, `ky`, and instances created with `axios.create` or `ky.create`, including instances imported from another module, whose URL argument is a string or template literal. The HTTP method comes from the client verb or a literal `method` option and defaults to `GET` for `fetch`. Template literals are recorded as patterns with each `${expression}` written as `{expression}`; nothing is substituted. A site's identity is the module, client, client instance, literal base URL, method, and URL, so two instances with different base URLs requesting the same path remain distinct. A `requests` edge connects the module to the site. URLs that are variables or expressions are `dynamic-routing` limitations.
- React is detected from a `package.json` that declares `react` and owns source modules, or from modules that import `react` explicitly. JSX alone is not a signal, because Preact, Solid, and other libraries use JSX too; such files are analyzed only when a package declares react.
- `service` nodes represent each `package.json` that declares `react` and owns source modules, with the declared version range, recognized toolchain, router, and HTTP client packages. A repository whose only React signal is source imports gets a `framework` node and a limitation whose evidence is the importing modules, and no service.
- Dynamic imports with literal specifiers produce both the `imports` edge and a `runtime-configuration` limitation; `eval` and `new Function` produce `reflection` limitations.

## Consequences

Request sites are declared targets, never observed traffic, and the generated documentation says so. Correlation can match `http_request` URLs and patterns against backend `endpoint` paths as candidates (ADR-003). JavaScript and TypeScript are read by a comment- and string-masking structural scanner, not a parser; quotes in operator position start strings and quotes after a word are JSX text, regex literals are not recognized, and what the scanner cannot classify is left out rather than guessed. The generated Markdown gains Routes, Outgoing HTTP requests, and Components sections, and the Services table summarizes whatever each adapter extracted. Schemas remain at version 1.0 because nothing is released (ADR-014).
