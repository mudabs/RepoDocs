package com.smartcart.order;

import java.util.List;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Modifying;
import org.springframework.data.jpa.repository.Query;

public interface OrderRepository extends JpaRepository<Order, Long> {

    List<Order> findByStatus(String status);

    @Modifying
    @Query("update Order o set o.status = 'ARCHIVED' where o.status = 'COMPLETED'")
    void archiveCompleted();
}
