import SwiftUI

struct PreferenceView: View {
    @State private var budget = 300.0
    @State private var avoidAlcohol = true
    @State private var preferLightweight = true
    @State private var selectedSkin = "油皮"
    private let skinTypes = ["油皮", "干皮", "混合肌", "敏感肌"]

    var body: some View {
        NavigationStack {
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 18) {
                    profileCard
                    preferenceCard
                    quickTags
                }
                .padding(20)
                .padding(.bottom, 104)
            }
            .background(AppTheme.background)
            .navigationTitle("个人偏好")
        }
    }

    private var profileCard: some View {
        HStack(spacing: 14) {
            Circle()
                .fill(LinearGradient(colors: [AppTheme.primary, Color(hex: "2563EB")], startPoint: .topLeading, endPoint: .bottomTrailing))
                .frame(width: 62, height: 62)
                .overlay(Text("L").font(.title2.bold()).foregroundStyle(.white))
            VStack(alignment: .leading, spacing: 4) {
                Text("Lily")
                    .font(.title3.bold())
                Text("偏好会用于导购检索和推荐排序")
                    .font(.caption)
                    .foregroundStyle(AppTheme.textSecondary)
            }
            Spacer()
        }
        .padding(16)
        .surfacePanel(cornerRadius: 24)
    }

    private var preferenceCard: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("购物偏好")
                .font(.headline)

            VStack(alignment: .leading, spacing: 8) {
                Text("常用预算：¥\(Int(budget))")
                    .font(.subheadline.weight(.medium))
                Slider(value: $budget, in: 50...1000, step: 50)
                    .tint(AppTheme.primary)
            }

            Picker("肤质", selection: $selectedSkin) {
                ForEach(skinTypes, id: \.self) { Text($0) }
            }
            .pickerStyle(.segmented)

            Toggle("避开含酒精商品", isOn: $avoidAlcohol)
                .tint(AppTheme.primary)
            Toggle("优先轻量/便携", isOn: $preferLightweight)
                .tint(AppTheme.primary)
        }
        .padding(16)
        .surfacePanel(cornerRadius: 24)
    }

    private var quickTags: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("关注品类")
                .font(.headline)
            FlowTags(tags: ["护肤", "数码", "运动", "通勤", "母婴", "家居"])
        }
        .padding(16)
        .surfacePanel(cornerRadius: 24)
    }
}

struct FlowTags: View {
    let tags: [String]

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 78), spacing: 10)], spacing: 10) {
            ForEach(tags, id: \.self) { tag in
                Text(tag)
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(AppTheme.primary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 10)
                    .background(AppTheme.secondary.opacity(0.78), in: Capsule())
            }
        }
    }
}
