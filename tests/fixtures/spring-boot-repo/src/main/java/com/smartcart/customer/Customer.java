package com.smartcart.customer;

import java.util.List;

import com.smartcart.order.Order;

import jakarta.persistence.Column;
import jakarta.persistence.Entity;
import jakarta.persistence.Id;
import jakarta.persistence.OneToMany;
import jakarta.persistence.Table;

@Entity
@Table(name = "customers")
public class Customer {

    @Id
    private Long id;

    @Column(name = "email_address", unique = true)
    private String email;

    @OneToMany(mappedBy = "customer")
    private List<Order> orders;
}
