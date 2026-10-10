//! The random numbers a building is laid out from.
//!
//! Same seed, same building, every time: a town rebuilt tomorrow has to come
//! out as the town it was. That is the whole requirement, and it is met by
//! seeding one generator per building from that building's own seed. Nothing
//! here is shared between threads, so two buildings laid out at the same time
//! cannot affect each other's draws however the pool hands them out, which is
//! what makes the result the same whether the map was built on 4 cores or 64.
//!
//! This is deliberately not CPython's Mersenne Twister. Matching Python draw
//! for draw would mean the same towns as before the port, and it was decided
//! that it is not worth carrying a slower generator and CPython's exact
//! rejection loops to get it. Buildings will differ from the Python pass; they
//! will not differ between two runs of this one.
//!
//! xoshiro256++ over a SplitMix64-expanded seed: a few instructions per draw,
//! and no measurable structure at the sizes a room layout asks for.

pub struct Rng {
    s: [u64; 4],
}

impl Rng {
    pub fn seeded(seed: u64) -> Rng {
        // SplitMix64, so that seeds one apart (which is how the storeys of one
        // building are seeded) start from unrelated states.
        let mut z = seed.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut next = || {
            z = z.wrapping_add(0x9E37_79B9_7F4A_7C15);
            let mut x = z;
            x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
            x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
            x ^ (x >> 31)
        };
        let mut s = [next(), next(), next(), next()];
        if s == [0, 0, 0, 0] {
            s[0] = 0x9E37_79B9_7F4A_7C15;
        }
        Rng { s }
    }

    #[inline]
    pub fn next_u64(&mut self) -> u64 {
        let result = self.s[0]
            .wrapping_add(self.s[3])
            .rotate_left(23)
            .wrapping_add(self.s[0]);
        let t = self.s[1] << 17;
        self.s[2] ^= self.s[0];
        self.s[3] ^= self.s[1];
        self.s[1] ^= self.s[2];
        self.s[0] ^= self.s[3];
        self.s[2] ^= t;
        self.s[3] = self.s[3].rotate_left(45);
        result
    }

    /// A number in [0, 1), as `random.random()` gives one: 53 bits.
    #[inline]
    pub fn random(&mut self) -> f64 {
        (self.next_u64() >> 11) as f64 * (1.0 / (1u64 << 53) as f64)
    }

    /// A number in `0..n`, with no bias towards the low end. Lemire's method:
    /// one multiply, and a second draw only in the rare uneven tail.
    pub fn below(&mut self, n: u64) -> u64 {
        if n <= 1 {
            return 0;
        }
        let mut wide = (self.next_u64() as u128) * (n as u128);
        let mut low = wide as u64;
        if low < n {
            let floor = n.wrapping_neg() % n;
            while low < floor {
                wide = (self.next_u64() as u128) * (n as u128);
                low = wide as u64;
            }
        }
        (wide >> 64) as u64
    }

    #[inline]
    pub fn below_usize(&mut self, n: usize) -> usize {
        self.below(n as u64) as usize
    }

    /// `random.randint(a, b)`: both ends included.
    pub fn randint(&mut self, a: i64, b: i64) -> i64 {
        if b <= a {
            return a;
        }
        a + self.below((b - a + 1) as u64) as i64
    }

    /// True with this chance, for the many `rng.random() < p` tests.
    #[inline]
    pub fn chance(&mut self, p: f64) -> bool {
        self.random() < p
    }

    /// `random.choice(seq)`. None where Python would raise, on an empty one.
    pub fn choice<'a, T>(&mut self, items: &'a [T]) -> Option<&'a T> {
        if items.is_empty() {
            return None;
        }
        let at = self.below_usize(items.len());
        items.get(at)
    }

    /// `random.shuffle(x)`: Fisher-Yates from the end.
    pub fn shuffle<T>(&mut self, items: &mut [T]) {
        if items.len() < 2 {
            return;
        }
        for i in (1..items.len()).rev() {
            let j = self.below_usize(i + 1);
            items.swap(i, j);
        }
    }

}
