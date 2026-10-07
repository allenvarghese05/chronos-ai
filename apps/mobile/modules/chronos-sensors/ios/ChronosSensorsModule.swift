import ExpoModulesCore

public class ChronosSensorsModule: Module {
  public func definition() -> ModuleDefinition {
    Name("ChronosSensors")

    // Returns a greeting string to prove the native bridge is working.
    // T2.1 will replace this with the full CoreMotion + device-state API.
    Function("hello") { () -> String in
      return "Hello from ChronosSensors!"
    }
  }
}
