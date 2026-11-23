package com.example.specimens;

import java.util.List;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

@RestController
@RequestMapping("/api/specimens")
public class SpecimenController {

    private final SpecimenService specimens;

    public SpecimenController(SpecimenService specimens) {
        this.specimens = specimens;
    }

    @GetMapping
    public List<String> list() {
        return specimens.names();
    }

    @PostMapping
    public void add(@RequestBody String name) {
        specimens.add(name);
    }
}
