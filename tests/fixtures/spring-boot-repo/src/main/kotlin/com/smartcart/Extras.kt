package com.smartcart

import org.springframework.web.bind.annotation.GetMapping
import org.springframework.web.bind.annotation.RestController

@RestController
class ExtrasController {
    @GetMapping("/api/extras")
    fun extras(): List<String> = listOf()
}
