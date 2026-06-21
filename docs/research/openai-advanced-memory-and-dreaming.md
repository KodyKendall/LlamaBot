https://openai.com/index/chatgpt-memory-dreaming/

Dreaming: Better memory for a more helpful ChatGPT
Improving memory synthesis in ChatGPT to optimize for freshness, continuity and relevance.


Listen to article
7:22
Share
Table of contents

How memory has evolved
How we evaluate memory
Carrying forward context
Following preferences
Staying current over time
A more scalable foundation for the future
Today we’re beginning to roll out a more capable and scalable system for synthesizing memory, developed to tackle the staleness, correctness, and scalability challenges that we observe when memory is applied to the hundreds of millions of users and multi-year time horizons in ChatGPT.

Memory is what helps ChatGPT learn your preferences, projects, and constraints, allowing future conversations to start from shared context rather than from scratch.

Over the last two years, memory has grown into a critical part of the ChatGPT experience, helping ChatGPT better understand your context so it can help you accomplish meaningful goals over time. This is central to making ChatGPT more useful: knowing you, helping you, and doing more for you.

This update is available to Plus and Pro users in the US today, and will roll out to additional countries and Free and Go users over the coming weeks.

How memory has evolved
Memory first launched in April 2024 (also known as saved memories). The feature let you ask ChatGPT to remember information and carry it forward into future chats.

Saved memories modal showing a searchable list of personal details, including work, interests, travel, and response preferences.
Saved memories were only written during the conversation and relied on strong cues to decide when to trigger memory, such as an instruction to "remember I’m traveling to Singapore in July." In practice, interacting with this system could feel like talking to someone who took a few notes, but still forgot everything that wasn’t written down. Saved memories also tend to go stale over time and eventually become incorrect or irrelevant.

In April 2025, we updated ChatGPT’s memory by giving the model the ability to reference chat context outside of the saved memories list; this was done by introducing the first version of dreaming—a method for ChatGPT to automatically curate memories in the background by referencing chat history.

Memory settings page in ChatGPT showing options to reference chat history, use saved memories, manage saved memories, and enable Pulse memory suggestions.
In contrast to saved memories, dreaming leverages a background process that allows ChatGPT to learn from many conversations and synthesize ChatGPT’s memory state in order to always provide the freshest, most relevant context to your conversations. Dreaming also makes it easier for memory to include context that occurs naturally in conversation, without relying on explicit requests to remember something.

Over the last year, dreaming supplemented saved memories to create a step-function improvement in ChatGPT's ability to personalize responses and offset the staleness of saved memories. However, it historically was never sufficient as a standalone memory system.

Today, we are launching a significantly more capable and compute-efficient memory architecture built on top of dreaming.

The memories synthesized by dreaming are reviewable through a summary of them made visible in the memory summary page. From the memory summary, you can quickly glean the highlights of what ChatGPT knows about you, add or update information about yourself, and provide instructions on what topics ChatGPT should bring up and when. If you want to drill down into a particular area to learn more, just chat with the model.

Memory summary modal showing a personalized overview of a user’s work, hobbies, travel interests, and community involvement, with options to correct or dismiss specific details.
How we evaluate memory
When we think about what "good memory" looks like in ChatGPT, a few things come to mind:

Carry forward useful context: You tell ChatGPT something once, and it remembers that information in your subsequent chats.
Follow preferences and constraints: If you describe a preference (e.g., you’re vegetarian), then ChatGPT should take actions that are consistent with that preference going forward.
Stay current over time: Memory should account for the passage of time. Imagine "The user is planning their birthday party for next Saturday"; eventually, Sunday arrives.
We can evaluate how ChatGPT Plus and Pro memory has improved over time with respect to each of the three memory objectives above. We do this for each of:

2024: Saved memories
2025: Saved memories + Dreaming V0
2026: Dreaming V3
Carrying forward context
When you start a new chat with ChatGPT, you don’t have to introduce yourself from scratch. ChatGPT can save you time and build on prior context, especially for complex, long-running projects.

For example, imagine you’re using ChatGPT to shop for new camera gear that's compatible with your current camera. If you've discussed your camera setup with ChatGPT in the past, you can ask for products that are compatible with "my photography setup" and get tailored recommendations that meet your needs.

Without memory
What would I need to buy to use TTL for my underwater photography setup?

To use TTL flash underwater, you need a compatible chain from camera → housing/trigger → cable → strobe. What you buy depends mostly on whether your setup uses fiber-optic TTL or electrical TTL.

Basic shopping checklist
TTL-capable underwater strobe
Look for strobes that support your TTL type, such as Inon S-TTL, Sea & Sea DS-TTL, Ikelite DS TTL, or compatible Backscatter/Nauticam systems. TTL has to be supported by the strobe, not just the camera.
TTL trigger or TTL converter
This is the key piece. It translates your camera’s flash metering into something the underwater strobe can understand. Examples include Nauticam optical TTL converters, Sea & Sea TTL converters, Backscatter Smart Control optical TTL triggers, or Ikelite DS Link TTL converters, depending on housing and camera brand. Backscatter notes that Nauticam optical TTL converters can work with fiber-optic-connected Inon and Sea & Sea TTL strobes, while Ikelite’s DS Link converters are made around Ikelite DS strobes and electrical sync setups. (Backscatter.com⁠(opens in a new window))
Connection cable
For fiber optic TTL, buy one fiber optic cable per strobe. Ikelite’s fiber-optic guide says each strobe needs its own light path, converter/adapter, and cord. (Ikelite⁠(opens in a new window))
For electrical TTL, buy the correct sync cord, such as Ikelite-style, Nikonos 5-pin, or housing-specific bulkhead cable.
Housing compatibility parts
You may need a hot-shoe adapter, bulkhead, fiber optic window/adapter, or housing-specific TTL board. Many TTL triggers mount inside the housing on the camera hot shoe.
Strobe arms, clamps, tray, and mounting hardware
TTL controls exposure, but you still need to physically mount and aim the strobes.
Batteries and spares
Bring strobe batteries, trigger batteries if required, spare fiber optic cables or sync cord O-rings, and silicone grease for serviceable O-rings.
Which route should you choose?
Fiber optic TTL is usually easiest if your housing has optical ports or can use the camera’s pop-up flash/LED trigger. It is common with compact, mirrorless, Nauticam, Sea & Sea, AOI, and Olympus/OM TG-style setups.

Electrical TTL is often best if you use Ikelite DS strobes or an Ikelite-style housing/bulkhead. Ikelite says its DS Link TTL converters are faster and more reliable than fiber-optic TTL triggers, and they do not require batteries. (Backscatter.com⁠(opens in a new window))

The compatibility rule
You need all three to match:

camera brand/protocol + housing/trigger type + strobe TTL system

For example:

Sony camera + Nauticam housing + optical TTL trigger + Inon/Sea & Sea-compatible fiber optic strobe
Nikon/Canon/Sony camera + Ikelite housing/bulkhead + Ikelite DS Link TTL converter + Ikelite DS strobe
Compact camera with built-in flash + fiber optic cable + S-TTL/DS-TTL-capable strobe
So the practical answer is: buy a TTL-compatible strobe, the correct TTL trigger/converter for your camera and housing, and the right fiber optic or electrical sync cable. The exact part numbers depend on your camera, housing, and strobe model.

The model produces a generic response that leaves the user to do complicated compatibility checks on their own.

With memory
What would I need to buy to use TTL for my underwater photography setup?

I’ll verify this against your actual setup: Sony A1 II in Nauticam NA-A1II, with Backscatter Mini Flash 3 and Inon Z-330 strobes. The key question is whether you want TTL with the Mini Flash 3, the Z-330s, or both.

For your setup — Sony A1 II + Nauticam NA-A1II + Backscatter Mini Flash 3 + Inon Z-330 — there are two different TTL paths:

Best buy for Mini Flash 3 TTL
Buy:

Backscatter Smart Control TTL LED Nauticam Flash Trigger for Sony
SKU: BS-TR-SN2

Backscatter lists it as custom-designed for Nauticam full-frame Sony housings, TTL-compatible with Mini Flash 3, and the NA-A1II housing page offers it as the “Backscatter Smart Control TTL Flash Trigger” option. (Backscatter.com⁠(opens in a new window))

You’d use normal optical fiber cables from the housing to the Mini Flash 3. On the MF-3, you’d use SC or SC Macro mode; Backscatter says SC / SC Macro are the TTL modes for Mini Flash 3, with SC Macro specifically tuned for close macro work. (Backscatter.com⁠(opens in a new window))

For Inon Z-330 TTL
For the Inon Z-330, the relevant unit is:

UW Technics 11075-HSS TTL Converter for Sony / Nauticam

UW Technics explicitly lists Inon Z-330 as a compatible TTL strobe and supports optical TTL output. (UWTechnics⁠(opens in a new window))

But I would not buy this for Mini Flash 3 TTL. Backscatter’s own UW Technics listing says Backscatter strobes are manual / HSS only on that converter, while the Backscatter Smart Control trigger is the one listed for Sony TTL with the Mini Flash 3. (Backscatter.com⁠(opens in a new window))

Practical recommendation
Since you shoot macro and have the Mini Flash 3, I’d buy the Backscatter Smart Control TTL LED Nauticam Flash Trigger for Sony — BS-TR-SN2.

The catch: there probably is no single trigger that gives true TTL for both the Mini Flash 3 and Inon Z-330 at the same time. The Backscatter trigger is the right TTL solution for the MF-3; the UW Technics converter is the right TTL solution for the Z-330. So choose based on which strobe you actually want TTL for.

For your use case, I’d prioritize Mini Flash 3 TTL / SC Macro and treat the Z-330s as manual when needed.

The model remembers the user’s camera setup and recommends a compatible product.

We can construct an eval from examples that resemble this where the model is asked to respond to a prompt that requires it to recall factual information about the user. The model is then rewarded if it responds in a way that correctly uses the relevant context. In this evaluation, the new dreaming-based system improves the model's ability to recall relevant facts.

Factual recall

0%
20%
40%
60%
80%
100%
Task success
2024
2025
2026
41.5%
67.9%
82.8%
Following preferences
Memory also helps ChatGPT respond in ways that better match your preferences and constraints.

Imagine that you’re planning a trip to Singapore. Two months before your trip, you ask ChatGPT to help with an itinerary. ChatGPT already knows from past travel planning that you enjoy wildlife photography, prefer hotels with strong AC, and enjoy a quiet dinner over a crowded bar.

Without memory
Help me plan a trip to Singapore. I'm going to be there for a work trip in July and will have a few days free at the end.

Great add-on city for a work trip: Singapore is compact, safe, excellent for solo exploring, and easy to do in 2–4 free days. July is hot and humid; expect daytime highs around 31–33°C / 88–91°F, warm nights, and sudden rain, so plan outdoor time early morning or after 5 p.m. and keep midday for museums, malls, cafés, or hotel downtime. Singapore’s official weather service notes typical daily highs of 31–33°C and lows of 23–25°C. (Singapore Weather Service⁠(opens in a new window))

memory-preferences-without
A strong 3-day plan
Day 1: Marina Bay, Gardens, and skyline
Start late afternoon at Marina Bay and walk the waterfront: Merlion Park, Helix Bridge, ArtScience Museum exterior, then into Gardens by the Bay. Book Cloud Forest, Flower Dome, and either the OCBC Skyway or Supertree Observatory if views matter to you. Gardens by the Bay lists Cloud Forest and Flower Dome as open daily 9 a.m.–9 p.m.; Supertree Observatory is also listed as 9 a.m.–9 p.m. with last admission at 8:30 p.m., but check maintenance closures before booking. (Gardens by the Bay⁠(opens in a new window))

Evening: stay for the Supertree light show area, then dinner at Lau Pa Sat for an easy first-night hawker experience near the CBD. It has 80+ stalls and local staples like satay, laksa, nasi lemak, bak kut teh, kaya toast, and char kway teow. (Laupasat⁠(opens in a new window))

Day 2: Culture neighborhoods and food
Morning: Chinatown. Visit Buddha Tooth Relic Temple, walk the shophouse streets, and eat at Maxwell Food Centre or Chinatown Complex Food Centre. Visit Singapore lists Maxwell, Chinatown Complex, Old Airport Road, Tiong Bahru Market, Newton, and others among popular hawker centres. (Visit Singapore⁠(opens in a new window))

Afternoon: Kampong Glam and Haji Lane for Sultan Mosque, murals, boutiques, and cafés. Then head to Little India around golden hour for temples, flower garlands, Tekka Centre, and dinner.

Evening: either cocktails around Ann Siang / Club Street, or keep it low-key with a night walk along the Singapore River: Boat Quay to Clarke Quay to Robertson Quay.

Day 3: Nature or beach day
Pick one based on your energy.

For nature: do Singapore Botanic Gardens early, then the National Orchid Garden, followed by lunch in Dempsey Hill or Tiong Bahru. This is the best “green Singapore” day without leaving the city.

For beach/resort mode: go to Sentosa. Choose the southern beaches, Fort Siloso Skywalk, or a beach club. I would skip trying to do every Sentosa attraction unless you love theme parks.

For local food focus: do Tiong Bahru Market for breakfast, National Gallery Singapore midday, then Old Airport Road Food Centre for dinner. The official events page also shows exhibitions and cultural events, so it is worth checking once your exact July dates are fixed. (Visit Singapore⁠(opens in a new window))

Best places to stay after the work part
Marina Bay / CBD is easiest if you want polished, walkable, skyline-heavy Singapore and minimal transit.

Chinatown / Telok Ayer / Tanjong Pagar is my top pick for a few free days: great food, MRT access, heritage streets, cocktail bars, and close to work districts.

Bugis / Kampong Glam is better if you want boutique hotels, cafés, street art, and a slightly more relaxed neighborhood feel.

Orchard is convenient for shopping and rainy-day malls, but less atmospheric.

What to book ahead
Book Gardens by the Bay conservatories/Skyway, any special restaurants, and possibly a cocktail bar. For hawker centres, do not over-plan; just go hungry, carry tissues or wet wipes, and be ready to share tables.

July packing notes
Bring breathable work clothes, comfortable walking shoes, a small umbrella, and a light layer for aggressive air-conditioning. I would also plan one “rain-flexible” indoor block each day, because showers can arrive suddenly even when the forecast looks fine.

My suggested balance: Day 1 Marina Bay/Gardens, Day 2 Chinatown–Kampong Glam–Little India, Day 3 Botanic Gardens or Sentosa. That gives you the Singapore highlights without turning your post-work break into another work project.

The model produces a generic response that is more touristy, doesn't help with hotel booking, and largely ignores the user's interests.

With memory
Help me plan a trip to Singapore. I'm going to be there for a work trip in July and will have a few days free at the end.

I’ll build this around your known constraints: a few free days after work, hot/humid July weather, wildlife/photography/nature bias, strong AC needs for sleep, and good but not overly social dining. I’ll verify current July logistics and then give a practical short itinerary rather than a generic Singapore list.

memory-preferences-with
Here’s how I’d structure it: 2–3 free days, mostly nature / wildlife / photography, with heat-managed mornings and AC breaks. July is hot and humid: Singapore’s July long-term stats are about 31.4°C / 88.5°F average daily max, 25.4°C / 77.7°F min, 146.6 mm rain, and 14 rain days, so outdoor plans should be early morning or evening, not midday. (National Environment Agency⁠(opens in a new window))

Best plan for 3 free days
Day 1 — Gardens by the Bay + Marina Bay, low-friction
Morning: Cloud Forest + Flower Dome. This is the obvious “touristy” pick, but it fits you well because it’s visually interesting, good for photography, and climate-controlled. Gardens by the Bay lists Cloud Forest and Flower Dome-type attractions running 9:00am–9:00pm, and Cloud Forest currently has the Jurassic World experience listed as ongoing. (Gardens by the Bay⁠(opens in a new window))

Late afternoon / evening: Supertree Grove + Borealis / light show. Gardens currently lists Borealis every Sat, Sun & Mon, 8:00–8:30pm at Supertree Grove, so try to align this with one of those nights. (Gardens by the Bay⁠(opens in a new window))

Dinner: do something easy nearby: Marina Bay / Tanjong Pagar / Telok Ayer. For you, I’d prioritize reservation-based restaurants with real tables over hawker centers on the first night, because you dislike cramped solo seating.

Day 2 — Mandai wildlife day
Morning to afternoon: Bird Paradise + River Wonders. Bird Paradise is probably the best fit photographically; River Wonders is less “wildlife photography” and more controlled exhibits, but it pairs well logistically. River Wonders is listed as daily 9:00am–6:00pm, last entry 5:00pm. (Mandai Wildlife Reserve⁠(opens in a new window))

Evening: Night Safari. This is the rare Singapore attraction that is genuinely better at night and avoids the worst heat. Night Safari is listed as daily 6:00pm–12:00am, last entry 11:15pm, with tram plus walking trails and over 900 nocturnal animals. (Mandai Wildlife Reserve⁠(opens in a new window))

This is a full day. I would not add Singapore Zoo unless you have 4 free days; Mandai can become exhausting fast.

Day 3 — Nature reserve / macro-ish walking + food
Early morning: Singapore Botanic Gardens or MacRitchie / Windsor Nature Park. For a few free days, Botanic Gardens is the safer pick: easier transport, less sweaty, better bailout options, and more consistent casual photography. MacRitchie / Windsor is better if you want more actual forest, but it is hotter, sweatier, and less predictable.

Midday: AC break.

Late afternoon: Joo Chiat / Katong or Chinatown / Telok Ayer for walking, architecture, coffee, and dinner. This gives you a different texture from Marina Bay without turning the day into a logistics project.

My ranking for you
Cloud Forest / Gardens by the Bay — best combination of photogenic, iconic, heat-managed.
Bird Paradise + Night Safari — best wildlife-focused day.
Botanic Gardens early morning — best low-stress outdoor nature.
Joo Chiat / Katong — best neighborhood walk.
Hawker centers — worth doing, but not ideal for your seating preferences unless timed off-peak.
What I’d avoid or deprioritize
I would skip Sentosa unless you specifically want beach/resort stuff. I would also skip a packed “greatest hits” day of Merlion / Orchard / Little India / Chinatown / Clarke Quay; it’s inefficient and hot. For you, the trip will be better if each day has one strong anchor plus one easy evening plan.

Hotel / sleep note
Since this is a work trip, your hotel may be fixed. If you can choose or extend at the same hotel, the hard gate is still AC. Singapore hotels are generally AC-heavy, but your requirement is unusually strict.

One thing I need to make this concrete
Send the exact free dates and whether your work hotel is fixed. Then I’d turn this into a day-by-day plan with restaurant targets and a hotel/AC callout.

The model produces a response that is personalized to the user’s interests in wildlife photography, quiet dinners, and their priorities when booking a hotel.

Preferences can take several forms:

Instructions for how ChatGPT should respond ("don't bring up Stan again").
Your personal preferences or constraints ("I’m vegetarian")
Implicit preferences that shape what’s relevant to you ("I live near San Francisco" → local options should be tailored to this area)
In developing the new memory system, we improved ChatGPT’s ability to apply relevant preferences from past conversations. Following the "I’m vegetarian" example above, we can evaluate whether the model correctly leverages memory to produce vegetarian-friendly dining options when a vegetarian user asks for meal prep suggestions.

Preference adherence

0%
20%
40%
60%
80%
100%
Task success
2024
2025
2026
31.4%
55.3%
71.3%
Staying current over time
Time doesn’t stop when your chat ends.

Traditional memory systems can become stale. For example, you tell ChatGPT "I’m in Singapore and need a dinner recommendation for tonight." Then, time passes, your trip ends, and you wonder why ChatGPT still thinks you’re in Singapore.

With dreaming, memories are automatically updated as time passes, allowing ChatGPT to revise its memory from "You’re going to Singapore in July" to "You went to Singapore in July 2026" when the trip ends. Then, when you’re back home, ChatGPT can again provide recommendations that are tailored to your home location and time zone.

Stale memory
Help me find something for take-out tonight that's still open.

It’s about 5:19 AM Sunday in Singapore, so your safest take-out/delivery bets are 24-hour places.

Best bets right now:

McDelivery / McDonald’s Singapore — easiest islandwide option; McDonald’s says McDelivery is available 24 hours, 7 days a week. Breakfast menu should be on now. (McDonald's Singapore⁠(opens in a new window))
Srisun Express — good for prata, Indian Muslim food, local comfort food; their site says they’re halal-certified and open 24/7. (Sri Sun Express⁠(opens in a new window))
89.7 Supper Club — good if you’re near Changi Village or Geylang East; their outlets page says both locations are open 24 hours, 7 days a week. (89.7 Supper Club⁠(opens in a new window))
126 Dim Sum Wen Dao Shi — worth checking on GrabFood/foodpanda if you’re in the East; a 2025 guide says it runs till 5:30 AM on weekdays and round-the-clock on weekends. Since it’s Sunday morning, it may still be available. (Eatbook⁠(opens in a new window))
Prata around Geylang / Serangoon / Bedok / Jalan Kayu — look in GrabFood or Google Maps for “prata open now”; guides list multiple 24-hour prata/supper spots, including Mufiz Prata Corner and RK Eating House. (Eatbook⁠(opens in a new window))
For the fastest path: open GrabFood / foodpanda / Deliveroo, set filter to Open now, then search “prata,” “McDonald’s,” “Srisun,” “89.7,” “dim sum,” or “nasi lemak.” If you tell me your neighborhood or nearest MRT, I’ll narrow it to specific nearby places.

The model thinks the user is still in Singapore.

With memory
Help me find something for take-out tonight that's still open.

I’ll use Portola Valley / Ladera as the starting point and prioritize places that are plausibly open for takeout tonight, then narrow to low-friction options rather than a huge list.

For tonight, Saturday May 30, near Ladera / Portola Valley, I’d start here:

Alpine Inn — safest casual pickup option. Open Saturday until 9:30 pm and has Order Takeout on its site. Good for burgers / tavern food. (Rossotti's Alpine Inn⁠(opens in a new window))
Taverna Portola Valley — nearby at Ladera Country Shopper; open daily, Saturday dinner until 9:00 pm. Better if you want Greek / Mediterranean. (TAVERNA Portola Valley⁠(opens in a new window))
Portola Bistro — also nearby; Saturday dinner until 9:30 pm. Good option for Italian / Mediterranean / pizza-ish dinner. (Portola Bistro -⁠(opens in a new window))
Amigos Grill — Mexican, very close in Ladera Country Shopper. Official site says call to order at 650-851-3997; DoorDash / order.online lists pickup/delivery ordering until 8:40 pm. (Amigos Grill⁠(opens in a new window))
State of Mind Slice House, Palo Alto — best fallback if you want pizza and can drive farther; Friday–Saturday hours are 11 am–10 pm. (State of Mind Slice House⁠(opens in a new window))
My pick: Alpine Inn for easiest local takeout, Taverna if you want something less heavy, State of Mind if it’s later and you want the most reliable post-9 pm option.

The model provides responses that are relevant to the user’s home location.

In our memory evaluations, we measure whether ChatGPT can correctly respond to prompts where the passage of time materially affects the correct answer or recommendation. Dreaming provides a substantial lift in this area:

Staying correct over time

0%
20%
40%
60%
80%
100%
Task success
2024
2025
2026
9.4%
52.2%
75.1%
A more scalable foundation for the future
At OpenAI, our mission is to ensure that artificial general intelligence benefits all of humanity.

While dreaming-based memory has been available to Plus and Pro users for some time, we are only now able to offer Free users a version that meets our quality bar and is practical to serve at scale. Recent improvements reduced the compute required to serve dreaming to Free users by approximately 5x, making it possible to begin rolling out dreaming to Free users over the coming weeks and to increase memory capacity for Plus and Pro users.

Looking ahead, dreaming now provides us with a shared memory foundation for all users. This update represents our most capable memory system yet, and we’ll continue improving it.

To learn more about this release and memory user controls, visit our Memory FAQ⁠(opens in a new window).