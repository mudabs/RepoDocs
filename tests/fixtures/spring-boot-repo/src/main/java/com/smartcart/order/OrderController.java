package com.smartcart.order;

import java.util.List;

import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.DeleteMapping;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestMethod;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * Orders API. Annotations inside comments such as @GetMapping("/ignored") must not be extracted.
 */
@RestController
@RequestMapping(path = "/api/orders", produces = {"application/json"})
public class OrderController {

    private static final String ARCHIVE_PATH = "/archive"; // used by a mapping below

    private final OrderService orderService;

    public OrderController(OrderService orderService) {
        this.orderService = orderService;
    }

    @GetMapping
    public List<Order> list(@RequestParam(required = false) String status) {
        String notARoute = "/api/orders/{not-a-route}";
        return orderService.findAll(status);
    }

    @GetMapping("/{id}")
    public ResponseEntity<Order> get(@PathVariable("id") Long id) {
        return ResponseEntity.of(orderService.findById(id));
    }

    @PostMapping(consumes = "application/json")
    public Order create(@RequestBody Order order) {
        return orderService.save(order);
    }

    @RequestMapping(method = {RequestMethod.PUT, RequestMethod.PATCH}, value = "/{id}")
    public Order update(@PathVariable Long id, @RequestBody Order order) {
        return orderService.save(order);
    }

    @DeleteMapping(ARCHIVE_PATH)
    public void archive() {
        orderService.archive();
    }
}
