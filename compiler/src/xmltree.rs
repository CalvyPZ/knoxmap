// SPDX-License-Identifier: GPL-2.0-or-later
// Copyright (C) Tim Baker
// Copyright (C) Unjammer and the PZ Mapping Tools contributors
//
// Modified version of PZWorldEd from PZ Mapping Tools commit
// 4e86b80c505b3d77a2fb5f5675b752da966306f6.

//! Small XML tree so the .pzw, .tbx, and .tmx readers share one path.

use std::collections::HashMap;

use quick_xml::events::Event;
use quick_xml::Reader;

#[derive(Clone, Debug)]
pub struct Elem {
    pub name: String,
    pub attrs: HashMap<String, String>,
    pub text: String,
    pub children: Vec<Elem>,
}

impl Elem {
    pub fn attr(&self, key: &str) -> &str {
        self.attrs.get(key).map(String::as_str).unwrap_or("")
    }

    pub fn children_named<'a>(&'a self, name: &'a str) -> impl Iterator<Item = &'a Elem> + 'a {
        self.children.iter().filter(move |c| c.name == name)
    }

    pub fn child(&self, name: &str) -> Option<&Elem> {
        self.children.iter().find(|c| c.name == name)
    }
}

pub fn parse(xml: &str) -> Result<Elem, String> {
    let mut reader = Reader::from_str(xml);
    reader.trim_text(false);
    let mut buf = Vec::new();
    let mut stack: Vec<Elem> = Vec::new();
    loop {
        match reader.read_event_into(&mut buf) {
            Ok(Event::Start(e)) => {
                stack.push(elem_from(e.name().as_ref(), e.attributes())?);
            }
            Ok(Event::Empty(e)) => {
                let node = elem_from(e.name().as_ref(), e.attributes())?;
                push_child(&mut stack, node)?;
            }
            Ok(Event::Text(e)) => {
                let text = e
                    .unescape()
                    .map_err(|err| err.to_string())?
                    .into_owned();
                if let Some(top) = stack.last_mut() {
                    top.text.push_str(&text);
                }
            }
            Ok(Event::CData(e)) => {
                let text = String::from_utf8_lossy(&e).into_owned();
                if let Some(top) = stack.last_mut() {
                    top.text.push_str(&text);
                }
            }
            Ok(Event::End(_)) => {
                let node = stack.pop().ok_or_else(|| "xml end without a start".to_string())?;
                if stack.is_empty() {
                    return Ok(node);
                }
                push_child(&mut stack, node)?;
            }
            Ok(Event::Eof) => {
                return stack
                    .pop()
                    .ok_or_else(|| "xml document has no root".to_string());
            }
            Err(err) => return Err(err.to_string()),
            _ => {}
        }
        buf.clear();
    }
}

fn push_child(stack: &mut [Elem], node: Elem) -> Result<(), String> {
    stack
        .last_mut()
        .ok_or_else(|| format!("xml element <{}> is outside the root", node.name))?
        .children
        .push(node);
    Ok(())
}

fn elem_from(
    name: &[u8],
    attrs: quick_xml::events::attributes::Attributes,
) -> Result<Elem, String> {
    let mut map = HashMap::new();
    for attr in attrs {
        let attr = attr.map_err(|err| err.to_string())?;
        let key = String::from_utf8_lossy(attr.key.as_ref()).into_owned();
        let val = attr.unescape_value().map_err(|err| err.to_string())?.into_owned();
        map.insert(key, val);
    }
    Ok(Elem {
        name: String::from_utf8_lossy(name).into_owned(),
        attrs: map,
        text: String::new(),
        children: Vec::new(),
    })
}
