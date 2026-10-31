use anyhow::{Result, bail};

pub fn vk(name: &str) -> Result<u32> {
    let n = name.trim().to_lowercase();
    let named = match n.as_str() {
        "backspace" => 0x08,
        "tab" => 0x09,
        "enter" => 0x0D,
        "shift" => 0x10,
        "ctrl" => 0x11,
        "alt" => 0x12,
        "esc" => 0x1B,
        "space" => 0x20,
        "pageup" => 0x21,
        "pagedown" => 0x22,
        "end" => 0x23,
        "home" => 0x24,
        "left" => 0x25,
        "up" => 0x26,
        "right" => 0x27,
        "down" => 0x28,
        "delete" => 0x2E,
        "win" => 0x5B,
        "apps" => 0x5D,
        _ => 0,
    };
    if named != 0 {
        return Ok(named);
    }
    let b = n.as_bytes();
    if b.len() == 1 && (b[0].is_ascii_digit() || b[0].is_ascii_lowercase()) {
        return Ok(b[0].to_ascii_uppercase() as u32);
    }
    if let Some(f) = n.strip_prefix('f').and_then(|d| d.parse::<u32>().ok()).filter(|f| (1..=12).contains(f)) {
        return Ok(0x70 + f - 1);
    }
    bail!("unknown key {name:?}")
}

pub fn chord(spec: &str) -> Result<Vec<u32>> {
    spec.split('+').map(vk).collect()
}

#[cfg(test)]
mod tests {
    #[test]
    fn keys() {
        assert_eq!(super::chord("Ctrl+Shift+s").unwrap(), vec![0x11, 0x10, 0x53]);
        assert_eq!(super::vk("F4").unwrap(), 0x73);
        assert!(super::vk("f13").is_err());
    }
}
