"""The small script of the static architecture export: diagram choice, zoom, pan, fit, and evidence details.

It builds the page with ``textContent`` and creates a link only when the URL matches the allowlist
below, which mirrors ``navigation.SOURCE_LINK``. It makes no network request.
"""

VIEWER_SCRIPT = r"""
(function () {
  'use strict';
  var data = JSON.parse(document.getElementById('data').textContent);
  var SOURCE_LINK = /^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+\/blob\/[0-9a-f]{40}\/[A-Za-z0-9._~%\/+@!$&'()*,;=:-]+(?:#L[0-9]+(?:-L[0-9]+)?)?$/;
  var stage = document.getElementById('stage');
  var canvas = document.getElementById('canvas');
  var details = document.getElementById('details');
  var select = document.getElementById('diagram');
  var state = { x: 0, y: 0, scale: 1 };

  function element(tag, text, className) {
    var node = document.createElement(tag);
    if (text !== undefined) { node.textContent = text; }
    if (className) { node.className = className; }
    return node;
  }

  function place() {
    canvas.style.transform = 'translate(' + state.x + 'px,' + state.y + 'px) scale(' + state.scale + ')';
  }

  function fit() {
    var svg = canvas.querySelector('svg');
    if (!svg) { return; }
    var box = svg.getBBox();
    var width = stage.clientWidth || 1;
    var height = stage.clientHeight || 1;
    svg.setAttribute('width', box.width + 40);
    svg.setAttribute('height', box.height + 40);
    svg.setAttribute('viewBox', (box.x - 20) + ' ' + (box.y - 20) + ' ' + (box.width + 40) + ' ' + (box.height + 40));
    svg.style.maxWidth = 'none';
    state.scale = Math.min(width / (box.width + 40), height / (box.height + 40), 1.5);
    state.x = (width - (box.width + 40) * state.scale) / 2;
    state.y = (height - (box.height + 40) * state.scale) / 2;
    place();
  }

  function zoom(factor, cx, cy) {
    var next = Math.max(0.1, Math.min(8, state.scale * factor));
    var ratio = next / state.scale;
    state.x = cx - (cx - state.x) * ratio;
    state.y = cy - (cy - state.y) * ratio;
    state.scale = next;
    place();
  }

  function link(label, url) {
    if (url && SOURCE_LINK.test(url)) {
      var anchor = element('a', label);
      anchor.href = url;
      anchor.target = '_blank';
      anchor.rel = 'noopener noreferrer';
      return anchor;
    }
    return element('span', label);
  }

  function evidenceList(items) {
    var list = element('ul', undefined, 'evidence');
    items.forEach(function (item) {
      var row = element('li');
      var lines = item.line_start ? ':' + item.line_start + (item.line_end > item.line_start ? '-' + item.line_end : '') : '';
      row.appendChild(link(item.repository_id + ':' + item.path + lines, item.link));
      var note = item.state === 'committed' ? '' : ' (working tree, no commit to link)';
      row.appendChild(element('small', note + ' sha256 ' + item.content_hash.slice(0, 12)));
      list.appendChild(row);
    });
    return list;
  }

  function show(title, lines, evidence, files) {
    details.textContent = '';
    details.appendChild(element('h2', title));
    lines.forEach(function (line) { details.appendChild(element('p', line)); });
    if (files && files.length) {
      details.appendChild(element('h3', 'Files'));
      var list = element('ul');
      files.forEach(function (file) { var row = element('li'); row.appendChild(link(file.path, file.link)); list.appendChild(row); });
      details.appendChild(list);
    }
    if (evidence && evidence.length) {
      details.appendChild(element('h3', 'Evidence'));
      details.appendChild(evidenceList(evidence));
    }
    details.appendChild(element('p', data.note, 'note'));
  }

  function showComponent(component) {
    var entry = data.evidence.components[component.id] || { files: [], evidence: [] };
    show(component.name, [
      component.kind + ', ' + component.classification + ', confidence ' + component.confidence + ', repository ' + component.repository,
      component.responsibility
    ], entry.evidence, entry.files);
  }

  function showConnection(connection) {
    var entry = data.evidence.connections[connection.id] || { evidence: [] };
    show(connection.source + ' to ' + connection.target, [
      connection.kind + ': ' + connection.label,
      connection.classification + ', confidence ' + connection.confidence + (connection.cross_repository ? ', across repositories' : '')
    ], entry.evidence, []);
  }

  function showKey(key) {
    var component = data.components.filter(function (item) { return item.key === key; })[0];
    if (component) { showComponent(component); return; }
    var group = data.groups[key];
    if (group) {
      show(group.label, ['Components in this group: ' + group.members.join(', ') + '. Choose a detail diagram or a component to see its evidence.'], [], []);
    }
  }

  function wire() {
    Array.prototype.forEach.call(canvas.querySelectorAll('g.node'), function (node) {
      var match = /(?:^|-)((?:c|g)_[0-9a-f]{12,16})(?:-|$)/.exec(node.id || '');
      if (match) {
        node.classList.add('clickable');
        node.addEventListener('click', function (event) { event.stopPropagation(); showKey(match[1]); });
      }
    });
  }

  var counter = 0;
  function draw(index) {
    counter += 1;
    var diagram = data.diagrams[index];
    return mermaid.render('diagram-' + counter, diagram.text).then(function (result) {
      canvas.innerHTML = result.svg;
      wire();
      fit();
    }).catch(function () {
      canvas.textContent = 'This diagram could not be drawn. Its Mermaid source is in ' + diagram.name + '.';
    });
  }

  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', htmlLabels: false, flowchart: { htmlLabels: false }, theme: 'default' });

  data.diagrams.forEach(function (diagram, index) {
    var option = element('option', diagram.name);
    option.value = String(index);
    select.appendChild(option);
  });
  select.addEventListener('change', function () { draw(Number(select.value)); });
  document.getElementById('fit').addEventListener('click', fit);
  document.getElementById('in').addEventListener('click', function () { zoom(1.25, stage.clientWidth / 2, stage.clientHeight / 2); });
  document.getElementById('out').addEventListener('click', function () { zoom(0.8, stage.clientWidth / 2, stage.clientHeight / 2); });
  stage.addEventListener('wheel', function (event) {
    event.preventDefault();
    var box = stage.getBoundingClientRect();
    zoom(event.deltaY < 0 ? 1.1 : 0.9, event.clientX - box.left, event.clientY - box.top);
  }, { passive: false });
  var drag = null;
  stage.addEventListener('mousedown', function (event) { drag = { x: event.clientX - state.x, y: event.clientY - state.y }; });
  window.addEventListener('mouseup', function () { drag = null; });
  window.addEventListener('mousemove', function (event) {
    if (drag) { state.x = event.clientX - drag.x; state.y = event.clientY - drag.y; place(); }
  });

  var list = document.getElementById('components');
  data.components.forEach(function (component) {
    var item = element('li');
    var button = element('button', component.name + ' (' + component.classification + ')');
    button.type = 'button';
    button.addEventListener('click', function () { showComponent(component); });
    item.appendChild(button);
    list.appendChild(item);
  });
  var links = document.getElementById('connections');
  data.connections.forEach(function (connection) {
    var item = element('li');
    var button = element('button', connection.source + ' to ' + connection.target + ': ' + connection.label + ' (' + connection.classification + ')');
    button.type = 'button';
    button.addEventListener('click', function () { showConnection(connection); });
    item.appendChild(button);
    links.appendChild(item);
  });
  var alternatives = document.getElementById('alternatives');
  if (!data.alternatives.length) { alternatives.appendChild(element('li', 'The model reported no alternative readings.', 'note')); }
  data.alternatives.forEach(function (alternative) {
    var item = element('li');
    var button = element('button', alternative.statement);
    button.type = 'button';
    button.addEventListener('click', function () {
      show('Alternative reading', [alternative.statement, 'About: ' + (alternative.about.join(', ') || 'the whole view')], alternative.evidence, []);
    });
    item.appendChild(button);
    alternatives.appendChild(item);
  });
  show('Select a component or connection', [data.banner], [], []);
  draw(0);
}());
"""
