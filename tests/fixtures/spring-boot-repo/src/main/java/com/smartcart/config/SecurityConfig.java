package com.smartcart.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

@Configuration
@Profile("prod")
public class SecurityConfig {

    @Bean
    public String securityMode() {
        return "strict";
    }
}
