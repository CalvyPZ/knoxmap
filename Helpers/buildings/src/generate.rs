//! One building, from its job record to its written .tbx.
//!
//! This is the only place a worker thread turns a job into a file, and the only
//! place a panic is caught: a building that hits an internal error comes back as
//! that one building failing, with its reason, and the pool carries on with the
//! rest of the map.
//!
//! The stages, in order: the walls it shares with its neighbours, the storeys
//! and the rooms in them, the doors, the windows, the furniture, then the file.
//! Nothing here calls back into Python and nothing here allocates anything the
//! thread does not reuse for the next building.

use std::fs;
use std::io::Write;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::path::Path;

use crate::blob::Blob;
use crate::catalog::Catalog;
use crate::furnish;
use crate::party;
use crate::results::Finished;
use crate::scratch::Scratch;
use crate::storeys::{self, Rules};
use crate::tbx;

pub enum Outcome {
    Built(Finished),
    /// Stop was asked while this building was being laid out. Nothing was
    /// written, and the worker puts the building back down.
    Cancelled,
}

pub fn one<F>(
    blob: &Blob,
    cat: &Catalog,
    index: &party::Index,
    at: usize,
    scratch: &mut Scratch,
    cancelled: F,
) -> Outcome
where
    F: Fn() -> bool,
{
    if cancelled() {
        return Outcome::Cancelled;
    }
    let job = blob.job(at);
    // This building's own data, read into buffers this thread already owns.
    // Nothing is allocated for a building no bigger than the last one.
    blob.mask_into(&job, &mut scratch.mask);
    party::edges_for(blob, index, at, &mut scratch.party);
    // What the map says is on the ground floor: the trades it recorded here.
    blob.uses_into(at, &mut scratch.uses);

    // `scratch` is reset before every building, so a panic part-way through one
    // cannot leave anything for the next to trip over; that is what makes
    // asserting unwind safety here true rather than hopeful.
    let built = catch_unwind(AssertUnwindSafe(|| {
        let rules = Rules::of(blob, &job);
        let mut built = storeys::build(
            &job,
            &scratch.mask,
            &scratch.party,
            &rules,
            cat,
            &scratch.uses,
        );
        for (level, storey) in built.storeys.iter_mut().enumerate() {
            let stairs = if level + 1 < built.stairs.len() + 1 {
                built.stairs.get(level).copied()
            } else {
                None
            };
            furnish::storey(storey, cat, stairs, &mut crate::rng::Rng::seeded(
                job.seed as u64 ^ ((level as u64 + 1) << 40),
            ));
        }
        let rooms = built.rooms();
        let furniture = built.furniture();
        let storeys_n = built.storeys.len();
        let doors = built.doors_out.clone();

        scratch.text.clear();
        tbx::render(
            &mut scratch.text,
            blob,
            cat,
            &built,
            blob.string(job.label),
            cat.style(job.style),
            job.kind,
            job.seed as u64,
        );
        (storeys_n, rooms, furniture, doors, scratch.text.len())
    }));
    let (storeys_n, rooms, furniture, doors, _len) = match built {
        Ok(item) => item,
        Err(_) => {
            return Outcome::Built(Finished::failed(
                at as u32,
                format!(
                    "{} ({}x{}, {} storeys): the building DLL hit an internal error",
                    blob.string(job.path),
                    job.width,
                    job.height,
                    job.levels
                ),
            ))
        }
    };

    // The file last, so a building that failed to lay out never leaves a
    // half-written .tbx behind for the map to load.
    let path = blob.string(job.path);
    if let Err(message) = write_file(path, &scratch.text) {
        return Outcome::Built(Finished::failed(at as u32, message));
    }
    Outcome::Built(Finished::ok(
        at as u32,
        storeys_n as u16,
        rooms as u32,
        furniture as u32,
        doors,
    ))
}

/// Write the document, then move it into place. A map being built is also being
/// read - the preview loads buildings as they appear - and a reader must never
/// see half a file.
fn write_file(path: &str, text: &str) -> Result<(), String> {
    if path.is_empty() {
        return Err("the building DLL was given no path to write to".to_string());
    }
    let final_path = Path::new(path);
    if let Some(parent) = final_path.parent() {
        if !parent.as_os_str().is_empty() {
            let _ = fs::create_dir_all(parent);
        }
    }
    let temp = final_path.with_extension("part");
    let wrote = fs::File::create(&temp).and_then(|mut file| {
        file.write_all(text.as_bytes())?;
        file.flush()
    });
    if let Err(why) = wrote {
        let _ = fs::remove_file(&temp);
        return Err(format!("{path}: {why}"));
    }
    // Windows will not rename onto an existing file.
    let _ = fs::remove_file(final_path);
    if let Err(why) = fs::rename(&temp, final_path) {
        let _ = fs::remove_file(&temp);
        return Err(format!("{path}: {why}"));
    }
    Ok(())
}
