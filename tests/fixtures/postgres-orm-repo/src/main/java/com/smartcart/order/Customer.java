package com.smartcart.order;

import jakarta.persistence.Entity;
import jakarta.persistence.Id;

/**
 * The table name is not declared, so no table can be linked to this entity.
 */
@Entity
public class Customer {

    @Id
    private Long id;
}
