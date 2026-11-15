package com.smartcart.order;

import java.util.List;
import java.util.Optional;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Service;

@Service
public class OrderService {

    private final OrderRepository orderRepository;

    @Value("${smartcart.orders.page-size:20}")
    private int pageSize;

    @Value("#{systemProperties['smartcart.region']}")
    private String region;

    public OrderService(OrderRepository orderRepository) {
        this.orderRepository = orderRepository;
    }

    public List<Order> findAll(String status) {
        return status == null ? orderRepository.findAll() : orderRepository.findByStatus(status);
    }

    public Optional<Order> findById(Long id) {
        return orderRepository.findById(id);
    }

    public Order save(Order order) {
        return orderRepository.save(order);
    }

    public void archive() {
        orderRepository.archiveCompleted();
    }
}
