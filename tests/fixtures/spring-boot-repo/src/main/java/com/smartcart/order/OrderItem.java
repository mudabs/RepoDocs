package com.smartcart.order;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.JoinColumn;
import jakarta.persistence.ManyToOne;

@Entity
public class OrderItem {

    @Id
    private Long id;

    private String sku;

    private int quantity;

    @ManyToOne
    @JoinColumn(name = "order_id")
    private Order order;
}
