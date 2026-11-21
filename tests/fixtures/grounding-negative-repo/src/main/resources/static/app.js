function show(names) {
  document.getElementById('specimens').textContent = names.join(', ');
}

fetch('/api/specimens').then((response) => response.json()).then(show);
