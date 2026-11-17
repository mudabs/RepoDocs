const express = require('express');
const storage = require('./lib/storage');

const app = express();
app.use(express.json());
app.use(express.static('public'));

app.get('/api/orders', (req, res) => res.json(storage.readOrders()));
app.post('/api/orders', (req, res) => {
  storage.appendOrder(req.body);
  res.status(201).end();
});

app.listen(3000);
