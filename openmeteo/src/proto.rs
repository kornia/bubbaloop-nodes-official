//! Locally generated protobuf types from protos/ directory.
//! These replace the bubbaloop-schemas dependency for weather types.

pub mod bubbaloop {
    pub mod header {
        pub mod v1 {
            include!(concat!(env!("OUT_DIR"), "/bubbaloop.header.v1.rs"));
        }
    }
    pub mod weather {
        pub mod v1 {
            include!(concat!(env!("OUT_DIR"), "/bubbaloop.weather.v1.rs"));
        }
    }
}

pub use bubbaloop::header::v1::Header;
pub use bubbaloop::weather::v1::*;

// ros-z type info implementations (enables ZPub/ZSub with ProtobufSerdes)
use ros_z::{MessageTypeInfo, TypeHash, WithTypeInfo};

macro_rules! impl_type_info {
    ($($type:ty => $name:literal),+ $(,)?) => {
        $(
            impl MessageTypeInfo for $type {
                fn type_name() -> &'static str { $name }
                fn type_hash() -> TypeHash { TypeHash::zero() }
            }
            impl WithTypeInfo for $type {}
        )+
    };
}

impl_type_info! {
    Header => "bubbaloop.header.v1.Header",
    CurrentWeather => "bubbaloop.weather.v1.CurrentWeather",
    HourlyForecast => "bubbaloop.weather.v1.HourlyForecast",
    DailyForecast => "bubbaloop.weather.v1.DailyForecast",
    LocationConfig => "bubbaloop.weather.v1.LocationConfig",
}
