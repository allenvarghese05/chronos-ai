require 'json'

package = JSON.parse(File.read(File.join(__dir__, '..', 'package.json')))

Pod::Spec.new do |s|
  s.name           = 'ChronosSensors'
  s.version        = package['version']
  s.summary        = package['description']
  s.description    = package['description']
  s.license        = package['license']
  s.author         = package['author'] || 'Chronos'
  s.homepage       = package['homepage'] || 'https://github.com/allenvarghese/chronos-ai'
  s.platforms      = { :ios => '17.0' }
  s.swift_version  = '5.9'
  s.source         = { git: '' }
  s.static_framework = true

  s.dependency 'ExpoModulesCore'

  # Include the Expo module wrapper files and the pure ChronosSensorsCore sources.
  # Paths are relative to this podspec (i.e. relative to ios/).
  # Tests/ are excluded — no XCTest framework available in Pod builds.
  s.source_files = [
    '*.swift',
    'ChronosSensorsCore/Sources/**/*.swift',
  ]

  # Required system frameworks for CoreMotion, UIKit and BGTaskScheduler.
  s.frameworks = ['CoreMotion', 'UIKit', 'BackgroundTasks']
end
