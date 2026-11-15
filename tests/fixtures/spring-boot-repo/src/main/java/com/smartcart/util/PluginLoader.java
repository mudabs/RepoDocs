package com.smartcart.util;

import org.springframework.stereotype.Component;

@Component
public class PluginLoader {

    public Object load(String className) throws Exception {
        Class<?> type = Class.forName(className);
        return type.getDeclaredConstructor().newInstance();
    }
}
