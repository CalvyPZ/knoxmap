//! Running the program over one band of rows.
//!
//! Every thread is handed the same program and a different band, and walks the
//! whole program in order drawing only the rows it owns. That is what keeps the
//! ground looking right: painting order is the one thing the Python renderer
//! depended on absolutely - a drive over a lawn, a kerb over the carriageway, a
//! tree over both - and an ordered walk per band preserves it exactly, while a
//! queue of shapes handed to whichever thread was free would not.
//!
//! The cost of handing every thread the whole program is one comparison a
//! command, because Python sends the rows each command can reach.

use crate::blob::{Blob, Cmd, Shape, TO_LAKE, TO_WATER};
use crate::err::Result;
use crate::raster::{self, Band, Paint, Span, Work};

/// What one thread keeps between commands, so that drawing ten million outlines
/// allocates nothing.
pub struct Scratch {
    pub work: Work,
    points: Vec<(f32, f32)>,
    spans: Vec<Span>,
}

impl Scratch {
    pub fn new() -> Scratch {
        Scratch {
            work: Work::new(),
            points: Vec::new(),
            spans: Vec::new(),
        }
    }

}

impl Default for Scratch {
    fn default() -> Scratch {
        Scratch::new()
    }
}

/// What a command puts down, resolved once.
pub fn paint_of(cmd: &Cmd) -> Paint {
    let mut paint = Paint::of(cmd.ground_colour(), cmd.veg_colour(), cmd.biome_index());
    paint.mask = cmd.mask_value();
    paint.water = cmd.to & TO_WATER != 0;
    paint.lake = cmd.to & TO_LAKE != 0;
    paint
}

/// Draw commands `from` up to `to` into this band.
pub fn draw(blob: &Blob, from: usize, to: usize, band: &mut Band, scratch: &mut Scratch) -> Result<()> {
    if let Some(sample) = band
        .ground
        .as_deref()
        .or(band.veg.as_deref())
        .or(band.biome.as_deref())
        .or(band.mask.as_deref())
    {
        crate::canvas::prepare_slice(sample, blob);
    }
    let (y0, y1) = (band.y0, band.y1);
    for at in from..to {
        let cmd = blob.cmd(at)?;
        if cmd.shape == Shape::Pass || !cmd.touches(y0, y1) {
            continue;
        }
        let paint = paint_of(&cmd);
        if paint.is_nothing() {
            continue;
        }
        match cmd.shape {
            Shape::Fill => {
                for which in 0..cmd.ring_n as usize {
                    load(blob, &cmd, which, scratch)?;
                    raster::fill(band, &scratch.points, &paint, &mut scratch.work);
                }
            }
            Shape::Stroke => {
                for which in 0..cmd.ring_n as usize {
                    load(blob, &cmd, which, scratch)?;
                    raster::stroke(band, &scratch.points, cmd.width, &paint, &mut scratch.work);
                }
            }
            Shape::Rect => {
                load(blob, &cmd, 0, scratch)?;
                if scratch.points.len() >= 2 {
                    let (x0, y0) = scratch.points[0];
                    let (x1, y1) = scratch.points[1];
                    raster::rect(band, x0, y0, x1, y1, &paint);
                }
            }
            Shape::Disc => {
                load(blob, &cmd, 0, scratch)?;
                if let Some((x, y)) = scratch.points.first().copied() {
                    raster::disc(band, x, y, cmd.width as f32 / 2.0, &paint);
                }
            }
            Shape::Relation => {
                load_all(blob, &cmd, scratch)?;
                let Scratch {
                    points,
                    spans,
                    work,
                } = scratch;
                raster::relation(band, points, spans, &paint, work);
            }
            Shape::Pass => {}
        }
    }
    Ok(())
}

/// One ring of a command, as tiles.
fn load(blob: &Blob, cmd: &Cmd, which: usize, scratch: &mut Scratch) -> Result<()> {
    scratch.points.clear();
    if which >= cmd.ring_n as usize {
        return Ok(());
    }
    let ring = blob.ring(cmd.ring_at as usize + which)?;
    scratch.points.reserve(ring.n as usize);
    for at in ring.at as usize..(ring.at + ring.n) as usize {
        scratch.points.push(blob.point(at));
    }
    Ok(())
}

/// Every ring of a relation, flat, with a span apiece saying which are holes.
fn load_all(blob: &Blob, cmd: &Cmd, scratch: &mut Scratch) -> Result<()> {
    scratch.points.clear();
    scratch.spans.clear();
    for which in 0..cmd.ring_n as usize {
        let ring = blob.ring(cmd.ring_at as usize + which)?;
        let at = scratch.points.len();
        for point in ring.at as usize..(ring.at + ring.n) as usize {
            scratch.points.push(blob.point(point));
        }
        scratch.spans.push(Span {
            at,
            n: ring.n as usize,
            inner: ring.role == crate::blob::ROLE_INNER,
        });
    }
    Ok(())
}
