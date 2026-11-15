package com.smartcart.config;

import static org.springframework.web.servlet.function.RouterFunctions.route;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.function.RequestPredicates;
import org.springframework.web.servlet.function.RouterFunction;
import org.springframework.web.servlet.function.ServerResponse;

@Configuration
public class FunctionalRoutes {

    @Bean
    public RouterFunction<ServerResponse> healthRoutes() {
        return route(RequestPredicates.GET("/health"), request -> ServerResponse.ok().build());
    }
}
