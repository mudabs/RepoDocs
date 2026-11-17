function render(orders) {
  document.getElementById('orders').innerHTML = orders.map((order) => '<li>' + order.item + '</li>').join('');
}

fetch('/api/orders').then((response) => response.json()).then(render);
