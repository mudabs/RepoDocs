const fs = require('fs');

const FILE = 'orders.json';

function readOrders() {
  return JSON.parse(fs.readFileSync(FILE, 'utf8'));
}

function appendOrder(order) {
  const orders = readOrders();
  orders.push(order);
  fs.writeFileSync(FILE, JSON.stringify(orders));
}

module.exports = { readOrders, appendOrder };
