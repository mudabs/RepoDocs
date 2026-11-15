package com.smartcart.order;

import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RestController;

@SpringBootTest
class OrderControllerTest {

    /** Test-only controller: the adapter must not extract it. */
    @RestController
    static class TestOnlyController {
        @GetMapping("/test-only")
        public String ping() {
            return "pong";
        }
    }
}
