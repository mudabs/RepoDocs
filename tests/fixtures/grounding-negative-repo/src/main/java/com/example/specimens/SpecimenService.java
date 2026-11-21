package com.example.specimens;

import java.util.ArrayList;
import java.util.List;
import org.springframework.stereotype.Service;

@Service
public class SpecimenService {

    private final List<String> names = new ArrayList<>();

    public List<String> names() {
        return names;
    }

    public void add(String name) {
        names.add(name);
    }
}
